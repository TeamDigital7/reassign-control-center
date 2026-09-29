"""Fuentes de stock. BigQuery es **solo lectura**: nunca se escribe alli.

Se ofrecen dos proveedores con la misma interfaz:

* `BigQueryStockSource` : consulta `stg_pe_central_stock_bi` (produccion).
* `ManualStockSource`   : lee un Excel/CSV de stock. Sirve para probar la
  app sin credenciales y para simulaciones puntuales del equipo.

Ambos devuelven un DataFrame con el mismo contrato:

    sku | cod_tienda | stock_tiendas | stock_bodega | stock | fecha_corte
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Protocol

import pandas as pd

from config import settings
from core.excel_io import as_text, normalize_sku, normalize_store_code, to_int

STOCK_COLUMNS = ["sku", "cod_tienda", "stock_tiendas", "stock_bodega", "stock", "fecha_corte"]

# --- Canonizacion del SKU dentro de BigQuery --------------------------------
# `SKU_LIMPIO_SQL` y `SKU_CANONICO_SQL` replican paso a paso lo que hace
# `core.excel_io.normalize_sku` del lado del archivo de pedidos: recortar el
# `.0` que deja un campo numerico, pasar a mayusculas, y quitar los ceros a la
# izquierda **solo** cuando el codigo es todo digitos.
#
# Sin esto el filtro `IN UNNEST(@skus)` compara la forma cruda de la tabla
# (`5438957.0` si `id_producto` es FLOAT, `0005438957` si es texto) contra el
# SKU ya normalizado del Excel: no devuelve ninguna fila y la app termina
# informando "sin stock" para productos que si lo tienen.
SKU_LIMPIO_SQL = "REGEXP_REPLACE(UPPER(TRIM(CAST({column} AS STRING))), r'[.]0+$', '')"
SKU_CANONICO_SQL = (
    "IF(REGEXP_CONTAINS({column}, r'^[0-9]+$'), "
    "IFNULL(REGEXP_EXTRACT({column}, r'^0*([0-9]+?)$'), {column}), "
    "{column})"
)

# --- Reservas ---------------------------------------------------------------
# El disponible real de un almacen es su stock MENOS lo reservado. Las columnas
# de reserva no tienen un nombre fijo en el origen, asi que se reconocen por el
# encabezado: cualquier columna que diga "reserva".
#
# Se reparten por lo que nombre el propio encabezado: si menciona bodega resta
# de bodega, si menciona tienda resta de tiendas, y una reserva sin apellido
# resta de las dos. Eso ultimo es seguro porque en cada fila solo se usa uno de
# los dos almacenes: en una bodega central manda `stock_bodega` y en una tienda
# fisica manda `stock_tiendas`.
RESERVE_HINT = "reserva"


def classify_reserve_columns(names: Iterable[str]) -> tuple[list[str], list[str]]:
    """Devuelve `(reservas_de_tiendas, reservas_de_bodega)` segun el encabezado."""
    de_tiendas: list[str] = []
    de_bodega: list[str] = []
    for name in names:
        texto = as_text(name).strip().lower()
        if RESERVE_HINT not in texto:
            continue
        menciona_bodega = "bodega" in texto or "almacen" in texto
        menciona_tienda = "tienda" in texto or "local" in texto
        if menciona_bodega and not menciona_tienda:
            de_bodega.append(name)
        elif menciona_tienda and not menciona_bodega:
            de_tiendas.append(name)
        else:
            # Sin apellido (o con los dos): aplica al almacen que use la fila.
            de_tiendas.append(name)
            de_bodega.append(name)
    return de_tiendas, de_bodega


def _net_sql(base_column: str, reserves: Iterable[str]) -> str:
    """`unidades(base) - unidades(reserva1) - ...` en SQL."""
    expresion = UNIDADES_SQL.format(column=base_column)
    for reserve in reserves:
        expresion += f" - {UNIDADES_SQL.format(column=reserve)}"
    return expresion


# --- Corte y unidades dentro de BigQuery ------------------------------------
# El corte se compara por DIA, no por instante. `fecha_corte` puede ser una
# marca de tiempo, y entonces `MAX(fecha_corte)` es un **instante**: si el ETL
# sella cada lote con su propia hora, unir por igualdad se queda con una
# rebanada minima de la foto (o con nada) y la app aparece sin stock aunque la
# tabla lo tenga. `DIA_CORTE_SQL` sirve para DATE, DATETIME, TIMESTAMP y texto
# ISO por igual.
DIA_CORTE_SQL = (
    "COALESCE("
    "SAFE_CAST(CAST({column} AS STRING) AS DATE), "
    "SAFE_CAST(LEFT(CAST({column} AS STRING), 10) AS DATE)"
    ")"
)

# Las unidades pasan por FLOAT64 antes de INT64: un `CAST('3.0' AS INT64)`
# directo **falla** en BigQuery y tumba la consulta entera si la columna llega
# como texto.
UNIDADES_SQL = "CAST(COALESCE(SAFE_CAST(CAST({column} AS STRING) AS FLOAT64), 0) AS INT64)"

# Consulta parametrizada. Se filtra por SKU para no traer la tabla completa y
# se toma unicamente el ultimo dia de `fecha_corte` disponible.
STOCK_QUERY = """
WITH con_dia AS (
  SELECT
    {sku_limpio}                     AS sku_limpio,
    CAST(s.codigo_tienda AS STRING)  AS cod_tienda,
    {neto_tiendas}                   AS stock_tiendas,
    {neto_bodega}                    AS stock_bodega,
    CAST(s.fecha_corte AS STRING)    AS fecha_corte,
    {dia_corte}                      AS dia_corte
  FROM `{table}` AS s
),
ultimo_corte AS (
  SELECT MAX(dia_corte) AS dia_corte
  FROM con_dia
),
corte_vigente AS (
  SELECT c.sku_limpio, c.cod_tienda, c.stock_tiendas, c.stock_bodega, c.fecha_corte
  FROM con_dia AS c
  JOIN ultimo_corte AS u
    ON c.dia_corte = u.dia_corte
),
stock_vigente AS (
  SELECT
    {sku_canonico} AS sku,
    cod_tienda,
    stock_tiendas,
    stock_bodega,
    fecha_corte
  FROM corte_vigente
)
SELECT
  sku,
  cod_tienda,
  SUM(stock_tiendas) AS stock_tiendas,
  SUM(stock_bodega)  AS stock_bodega,
  MAX(fecha_corte)   AS fecha_corte
FROM stock_vigente
WHERE sku IN UNNEST(@skus)
GROUP BY sku, cod_tienda
"""


def build_stock_query(table: str, columns: Iterable[str] = ()) -> str:
    """Consulta por defecto, ya formateada para una tabla concreta.

    `columns` son los nombres de columna de la tabla. Con ellos se detectan
    las de reserva y se descuentan dentro de la consulta, para que
    `stock_tiendas` y `stock_bodega` lleguen ya netos. Sin `columns` la
    consulta es la de siempre y no resta nada.
    """
    de_tiendas, de_bodega = classify_reserve_columns(columns)
    return STOCK_QUERY.format(
        table=table,
        sku_limpio=SKU_LIMPIO_SQL.format(column="s.id_producto"),
        sku_canonico=SKU_CANONICO_SQL.format(column="sku_limpio"),
        neto_tiendas=_net_sql("s.stock_tiendas", (f"s.{c}" for c in de_tiendas)),
        neto_bodega=_net_sql("s.stock_bodega", (f"s.{c}" for c in de_bodega)),
        dia_corte=DIA_CORTE_SQL.format(column="s.fecha_corte"),
    )


# BigQuery limita el tamano de los parametros; los SKU se mandan por lotes.
SKU_BATCH_SIZE = 5000

# Anchos habituales de un codigo de producto guardado como texto con ceros a
# la izquierda. Se usan solo para armar variantes de busqueda, nunca para
# escribir un SKU.
SKU_PADDED_WIDTHS = (8, 10, 12, 13)


def sku_variants(sku: str) -> list[str]:
    """Formas crudas en que un mismo SKU puede estar guardado en la tabla.

    La consulta propia del repo canoniza los dos lados y no necesita esto.
    Pero un `stock_query` de los secrets se ejecuta **tal cual**: si compara
    `CAST(id_producto AS STRING)` en crudo contra el SKU ya normalizado del
    Excel, no devuelve ninguna fila y la app informa "sin stock" con la tabla
    llena. Mandar tambien las variantes hace que ese cruce funcione sin tener
    que reescribir la consulta del usuario.
    """
    canonico = normalize_sku(sku)
    if not canonico:
        return []
    variantes = [canonico]
    if canonico.isdigit():
        variantes.append(f"{canonico}.0")
        variantes.extend(
            canonico.rjust(ancho, "0") for ancho in SKU_PADDED_WIDTHS if len(canonico) < ancho
        )
    return list(dict.fromkeys(variantes))


class StockSource(Protocol):
    name: str

    def fetch(self, skus: Iterable[str]) -> pd.DataFrame: ...


def empty_stock_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=STOCK_COLUMNS)


# Nombres que puede traer una consulta de stock segun de donde venga.
# `id_producto` / `codigo_tienda` son los de `stg_pe_central_stock_bi`, que es
# lo que devuelve el `stock_query` de Catalogo Control Center si se reutiliza.
QUERY_COLUMN_ALIASES = {
    "sku": ("sku", "id_producto", "codint_ma"),
    "cod_tienda": ("cod_tienda", "codigo_tienda", "bodega", "numbodega"),
    "stock_tiendas": ("stock_tiendas", "stock_tienda"),
    "stock_bodega": ("stock_bodega",),
    "fecha_corte": ("fecha_corte", "fecha"),
}


def _rename_query_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Acepta el esquema de `stg_pe_central_stock_bi` ademas del propio."""
    lookup = {str(column).strip().lower(): column for column in df.columns}
    mapping = {}
    for target, aliases in QUERY_COLUMN_ALIASES.items():
        if target in df.columns:
            continue
        for alias in aliases:
            if alias in lookup:
                mapping[lookup[alias]] = target
                break
    return df.rename(columns=mapping) if mapping else df


def _parse_cutoffs(values: pd.Series) -> pd.Series:
    """Parsea `fecha_corte` a datetime sin lanzar nunca.

    `pd.to_datetime(..., format="mixed")` **si lanza** con `errors="coerce"`
    cuando los textos traen offsets UTC distintos ("Mixed timezones detected"),
    y eso tumbaba la consulta de stock entera. Por eso se reintenta con
    `utc=True` y, si tampoco, se devuelve todo NaT para que el llamador caiga
    a la comparacion textual.
    """
    textos = values.map(as_text)
    for utc in (False, True):
        try:
            return pd.to_datetime(
                textos, errors="coerce", dayfirst=True, format="mixed", utc=utc
            )
        except (ValueError, TypeError):
            continue
    return pd.Series(pd.NaT, index=values.index)


def cutoff_days(values: pd.Series) -> pd.Series:
    """Dia del corte de cada fila (medianoche), o NaT si no se pudo leer."""
    fechas = _parse_cutoffs(values)
    if fechas.isna().all():
        return fechas
    return fechas.dt.normalize()


def latest_cutoff_value(values: pd.Series) -> Any:
    """Ultima fecha de corte, comparando como fecha y no como texto.

    Importa el detalle: si `fecha_corte` llega como texto en formato
    `DD/MM/YYYY`, comparar alfabeticamente elige mal — `31/12/2025` es mayor
    que `20/08/2026` como cadena. Por eso se parsea antes de comparar, y solo
    si eso falla se cae al maximo textual.
    """
    textos = values.map(as_text)
    textos = textos[textos != ""]
    if textos.empty:
        return None
    fechas = _parse_cutoffs(textos)
    if fechas.notna().any():
        return textos[fechas == fechas.max()].iloc[0]
    return textos.max()


def keep_latest_cutoff(df: pd.DataFrame) -> tuple[pd.DataFrame, int, str]:
    """Deja solo las filas del ultimo corte. Devuelve `(df, descartadas, corte)`.

    El stock es una foto, no un acumulado: si el origen trae historico, sumar
    todos los cortes multiplica las unidades disponibles e inventa stock que
    no existe.

    El corte se decide **por dia, no por instante**. Si `fecha_corte` es una
    marca de tiempo y el origen sella cada lote con su propia hora, quedarse
    solo con el instante maximo borra casi toda la foto: la app se queda sin
    stock aunque BigQuery lo tenga. Elegir entre varias fotos del mismo dia es
    trabajo de `consolidate`, que lo hace por (SKU, tienda) y por eso no
    descarta las filas de las demas tiendas.
    """
    if df.empty or "fecha_corte" not in df.columns:
        return df, 0, ""
    textos = df["fecha_corte"].map(as_text)
    if not bool((textos != "").any()):
        return df, 0, ""

    dias = cutoff_days(df["fecha_corte"])
    vigentes = (dias == dias.max()) if dias.notna().any() else (textos == textos.max())

    descartadas = int((~vigentes).sum())
    vigente_df = df[vigentes].reset_index(drop=True)
    corte = latest_cutoff_value(vigente_df["fecha_corte"]) if not vigente_df.empty else None
    return vigente_df, descartadas, as_text(corte)


# Formas validas de combinar los dos almacenes de una bodega central. Cual
# corresponde depende de como modela el origen: no hay una universalmente
# correcta, por eso se elige en la hoja Parametros y no en el codigo.
CENTRAL_FORMULAS = ("sumar", "solo_bodega", "restar_tiendas")


DEFAULT_CENTRAL_FORMULA = "solo_bodega"


def central_formula(value: Any = None) -> str:
    """Normaliza el nombre de la formula; lo desconocido cae en el default."""
    texto = as_text(value).strip().lower().replace(" ", "_").replace("-", "_")
    return texto if texto in CENTRAL_FORMULAS else DEFAULT_CENTRAL_FORMULA


def central_warehouse_codes(codes: Iterable[str] | None = None) -> set[str]:
    """Bodegas donde `stock_bodega` si es despachable. Por defecto, la 320."""
    if codes is None:
        codes = settings.CENTRAL_WAREHOUSE_CODES
    if isinstance(codes, str):
        codes = codes.replace(";", ",").split(",")
    resolved = {normalize_store_code(code) for code in codes}
    return {code for code in resolved if code}


def consolidate(df: pd.DataFrame) -> pd.DataFrame:
    """Una sola fila por par (SKU, tienda), dentro del corte vigente.

    Dentro del dia del corte pueden convivir dos cosas distintas, y hay que
    tratarlas al reves una de la otra:

    * el mismo par con la **misma** marca de tiempo repetido: es un solo corte
      abierto por talla, almacen o linea, y ahi se **suma** — sin esto
      `build_stock_index` se quedaba con la ultima fila y perdia unidades
      reales;
    * el mismo par con **varias** marcas de tiempo: son dos fotos del mismo
      dia, y la nueva **reemplaza** a la vieja — sumarlas inventaria stock.

    Distinguirlo por (SKU, tienda) —y no para toda la tabla— es lo que evita
    que una tienda sellada una hora antes desaparezca del stock disponible.
    Deja en `attrs["filas_reemplazadas_en_el_dia"]` cuantas filas quedaron
    fuera por ser una foto vieja del mismo dia.
    """
    if df.empty:
        return df
    atributos = dict(df.attrs)

    trabajo = df
    reemplazadas = 0
    fechas = _parse_cutoffs(df["fecha_corte"])
    if fechas.notna().any():
        trabajo = df.copy()
        trabajo["_ts"] = fechas
        maximos = trabajo.groupby(["sku", "cod_tienda"])["_ts"].transform("max")
        # Un par entero sin fecha legible se conserva completo.
        vigentes = trabajo["_ts"].eq(maximos) | (trabajo["_ts"].isna() & maximos.isna())
        reemplazadas = int((~vigentes).sum())
        trabajo = trabajo[vigentes].drop(columns="_ts")

    consolidado = (
        trabajo.groupby(["sku", "cod_tienda"], as_index=False, sort=False)
        .agg(
            stock_tiendas=("stock_tiendas", "sum"),
            stock_bodega=("stock_bodega", "sum"),
            stock=("stock", "sum"),
            fecha_corte=("fecha_corte", "max"),
        )[STOCK_COLUMNS]
    )
    consolidado.attrs.update(atributos)
    consolidado.attrs["filas_reemplazadas_en_el_dia"] = reemplazadas
    return consolidado


def _finalize(
    df: pd.DataFrame,
    include_central_warehouse: bool,
    central_codes: Iterable[str] | None = None,
    formula: Any = None,
) -> pd.DataFrame:
    """Normaliza tipos y calcula la columna `stock` efectiva."""
    # Se guarda cuantas filas entraron: en pantalla, "cero unidades" se ve
    # igual si la fuente no devolvio nada o si el saneamiento se lo llevo todo.
    filas_crudas = int(len(df))
    if df.empty:
        vacio = empty_stock_frame()
        vacio.attrs["filas_crudas"] = filas_crudas
        return vacio

    df = _rename_query_columns(df.copy())
    missing = [name for name in ("sku", "cod_tienda") if name not in df.columns]
    if missing:
        raise ValueError(
            "La consulta de stock no devolvio las columnas necesarias "
            f"({', '.join(missing)}). Columnas recibidas: {', '.join(map(str, df.columns))}."
        )
    df["sku"] = df["sku"].map(normalize_sku)
    df["cod_tienda"] = df["cod_tienda"].map(normalize_store_code)

    # Reservas que llegaron como columnas propias (archivo de stock, o una
    # consulta que las devuelve sin descontar). La consulta del repo ya las
    # resta dentro de BigQuery, asi que ahi no queda ninguna por descontar y
    # esto no hace nada.
    reservas_tiendas, reservas_bodega = classify_reserve_columns(df.columns)

    for column in ("stock_tiendas", "stock_bodega"):
        if column not in df.columns:
            df[column] = 0
        df[column] = pd.to_numeric(df[column], errors="coerce").fillna(0).astype(int)

    for column, reservas in (
        ("stock_tiendas", reservas_tiendas),
        ("stock_bodega", reservas_bodega),
    ):
        for reserva in reservas:
            df[column] = df[column] - pd.to_numeric(
                df[reserva], errors="coerce"
            ).fillna(0).astype(int)
    if "fecha_corte" not in df.columns:
        df["fecha_corte"] = ""
    df["fecha_corte"] = df["fecha_corte"].map(as_text)

    # En una tienda fisica el `stock_bodega` corresponde a otro almacen y no es
    # despachable desde ahi, asi que ahi siempre manda `stock_tiendas` solo.
    #
    # En una bodega central no hay una unica respuesta correcta: depende de si
    # el origen guarda los dos almacenes por separado (hay que sumar), si
    # `stock_bodega` ya incluye el piso (hay que tomar solo bodega) o si lo de
    # piso ya salio del total (hay que restar). Por eso la formula se elige en
    # la hoja Parametros; el default `sumar` es el comportamiento de siempre.
    central = df["cod_tienda"].isin(central_warehouse_codes(central_codes))
    df["stock"] = df["stock_tiendas"]
    if include_central_warehouse:
        modo = central_formula(formula)
        if modo == "solo_bodega":
            en_central = df["stock_bodega"]
        elif modo == "restar_tiendas":
            en_central = df["stock_bodega"] - df["stock_tiendas"]
        else:
            en_central = df["stock_tiendas"] + df["stock_bodega"]
        df["stock"] = en_central.where(central, df["stock_tiendas"])
    df["stock"] = df["stock"].astype(int)

    df = df[df["sku"] != ""]
    df = df[STOCK_COLUMNS].reset_index(drop=True)

    # Garantia unica para las dos fuentes: nunca se mezclan cortes. Si el
    # origen trae historico (o una consulta propia sin filtro de fecha), aqui
    # se queda solo la foto mas reciente.
    df, descartadas, corte = keep_latest_cutoff(df)

    # Recien con un solo corte sobre la mesa se suman las filas repetidas del
    # mismo par (SKU, tienda). Antes de filtrar por fecha, sumarlas mezclaria
    # fotos de dias distintos.
    df = consolidate(df)

    # El piso en cero va **al final**: una tienda con +5 en una fila y -3 en
    # otra tiene 2 unidades, no 5. Recortar antes de sumar inflaria el stock.
    if not df.empty:
        df["stock"] = df["stock"].clip(lower=0).astype(int)
    reemplazadas = int(df.attrs.get("filas_reemplazadas_en_el_dia", 0))
    df.attrs["filas_crudas"] = filas_crudas
    df.attrs["filas_descartadas_por_fecha"] = descartadas + reemplazadas
    df.attrs["filas_de_cortes_anteriores"] = descartadas
    df.attrs["filas_reemplazadas_en_el_dia"] = reemplazadas
    df.attrs["fecha_corte"] = corte
    return df


@dataclass
class BigQueryStockSource:
    """Consulta de stock contra BigQuery. Estrictamente de lectura."""

    project_id: str = ""
    job_project_id: str = ""
    table: str = settings.DEFAULT_STOCK_TABLE
    location: str = ""
    service_account_info: dict[str, Any] | None = None
    include_central_warehouse: bool = True
    central_codes: tuple[str, ...] = ()
    central_formula: str = DEFAULT_CENTRAL_FORMULA
    custom_query: str = ""
    name: str = "BigQuery"

    def _client(self):
        try:
            from google.cloud import bigquery
            from google.oauth2 import service_account
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Falta instalar google-cloud-bigquery. Ejecuta: pip install -r requirements.txt"
            ) from exc

        credentials = None
        project = self.project_id
        if self.service_account_info:
            credentials = service_account.Credentials.from_service_account_info(
                dict(self.service_account_info)
            )
            project = project or credentials.project_id
        return bigquery.Client(
            project=(self.job_project_id or project) or None,
            credentials=credentials,
        )

    def test_connection(self) -> tuple[bool, str]:
        """Valida credenciales y acceso a la tabla sin traer datos."""
        try:
            client = self._client()
            table = client.get_table(self.table)
            return True, f"Conectado. {table.full_table_id} ({table.num_rows:,} filas)."
        except Exception as exc:
            return False, str(exc)

    def table_columns(self) -> list[str]:
        """Nombres de columna de la tabla de stock. Vacio si no se pueden leer.

        Es una llamada de metadatos, no trae filas. Sirve para descubrir las
        columnas de reserva sin que nadie tenga que declararlas a mano; si
        falla (permisos, tabla ausente), la consulta sigue sin restar nada en
        vez de romperse.
        """
        try:
            return [campo.name for campo in self._client().get_table(self.table).schema]
        except Exception:
            return []

    def fetch(self, skus: Iterable[str]) -> pd.DataFrame:
        from google.cloud import bigquery

        unique = sorted({normalize_sku(sku) for sku in skus if normalize_sku(sku)})
        if not unique:
            return empty_stock_frame()

        client = self._client()
        query = self.custom_query.strip()
        if query:
            if "{table}" in query:
                query = query.format(table=self.table)
        else:
            query = build_stock_query(self.table, self.table_columns())

        # Una consulta propia puede no filtrar por SKU (por ejemplo, la de
        # Catalogo Control Center, que trae el corte completo). En ese caso se
        # ejecuta una sola vez, sin parametros, y se filtra despues en memoria.
        filters_by_sku = "@skus" in query

        # La consulta del repo canoniza el SKU dentro de BigQuery, asi que le
        # basta el valor canonico. Una consulta propia se ejecuta tal cual y
        # puede estar comparando el valor crudo: ahi se mandan las variantes.
        if filters_by_sku and self.custom_query.strip():
            a_buscar = list(dict.fromkeys(v for sku in unique for v in sku_variants(sku)))
        else:
            a_buscar = unique

        batches = (
            [a_buscar[start : start + SKU_BATCH_SIZE] for start in range(0, len(a_buscar), SKU_BATCH_SIZE)]
            if filters_by_sku
            else [None]
        )

        frames: list[pd.DataFrame] = []
        for batch in batches:
            job_config = bigquery.QueryJobConfig(
                use_legacy_sql=False,
                query_parameters=(
                    [bigquery.ArrayQueryParameter("skus", "STRING", batch)] if batch is not None else []
                ),
            )
            job = client.query(query, job_config=job_config, location=self.location or None)
            frames.append(job.result().to_dataframe())

        combined = pd.concat(frames, ignore_index=True) if frames else empty_stock_frame()
        result = _finalize(
            combined, self.include_central_warehouse, self.central_codes or None,
            self.central_formula,
        )
        if not filters_by_sku and not result.empty:
            result = result[result["sku"].isin(unique)].reset_index(drop=True)
        result.attrs["skus_solicitados"] = len(unique)
        result.attrs["tabla"] = self.table
        return result

    def diagnose(self, skus: Iterable[str]) -> dict[str, Any]:
        """Por que la consulta no trae stock. Solo lectura, sin traer datos.

        Cuando la app muestra cero unidades hay varias causas posibles que se
        ven identicas en pantalla: la tabla vacia, un corte nuevo sin filas,
        SKU que no existen, o SKU que solo estan en cortes viejos. Esto las
        separa con contadores concretos, usando la **misma** canonizacion de
        SKU que la consulta real para que el resultado sea comparable.
        """
        from google.cloud import bigquery

        unique = sorted({normalize_sku(sku) for sku in skus if normalize_sku(sku)})
        muestra = unique[:200]
        limpio = SKU_LIMPIO_SQL.format(column="s.id_producto")
        sql = f"""
        WITH con_dia AS (
          SELECT
            {SKU_CANONICO_SQL.format(column=limpio)} AS sku,
            CAST(s.id_producto AS STRING)            AS sku_crudo,
            {DIA_CORTE_SQL.format(column='s.fecha_corte')} AS dia_corte
          FROM `{self.table}` AS s
        ),
        corte AS (SELECT MAX(dia_corte) AS dia FROM con_dia)
        SELECT
          (SELECT COUNT(*) FROM con_dia)                            AS filas_tabla,
          (SELECT CAST(dia AS STRING) FROM corte)                   AS ultimo_corte,
          (SELECT COUNT(*) FROM con_dia n, corte c
             WHERE n.dia_corte = c.dia)                             AS filas_ultimo_corte,
          (SELECT COUNT(DISTINCT n.sku) FROM con_dia n
             WHERE n.sku IN UNNEST(@skus))                          AS skus_en_tabla,
          (SELECT COUNT(DISTINCT n.sku) FROM con_dia n, corte c
             WHERE n.dia_corte = c.dia AND n.sku IN UNNEST(@skus))  AS skus_en_ultimo_corte,
          (SELECT COUNT(DISTINCT n.sku_crudo) FROM con_dia n
             WHERE n.sku_crudo IN UNNEST(@skus))                    AS skus_sin_normalizar
        """
        job_config = bigquery.QueryJobConfig(
            use_legacy_sql=False,
            query_parameters=[bigquery.ArrayQueryParameter("skus", "STRING", muestra)],
        )
        client = self._client()
        job = client.query(sql, job_config=job_config, location=self.location or None)
        fila = dict(next(iter(job.result())).items())
        fila["skus_consultados"] = len(muestra)
        fila["tabla"] = self.table
        fila["consulta_propia"] = bool(self.custom_query)
        return fila


@dataclass
class ManualStockSource:
    """Stock desde un archivo subido por el usuario (modo sin BigQuery)."""

    frame: pd.DataFrame
    include_central_warehouse: bool = True
    central_codes: tuple[str, ...] = ()
    central_formula: str = DEFAULT_CENTRAL_FORMULA
    name: str = "Archivo de stock"

    COLUMN_ALIASES = {
        "sku": ["sku", "id_producto", "codint_ma", "codigo_sku", "cod_sku"],
        "cod_tienda": [
            "cod_tienda",
            "codigo_tienda",
            "bodega",
            "numbodega",
            "tienda",
            "store",
            "codigo",
        ],
        "stock_tiendas": ["stock_tiendas", "stock_tienda", "stock", "disponible", "stock_disponible"],
        "stock_bodega": ["stock_bodega", "stock_almacen"],
        "fecha_corte": ["fecha_corte", "fecha", "fecha_stock"],
    }

    def fetch(self, skus: Iterable[str]) -> pd.DataFrame:
        if self.frame is None or self.frame.empty:
            return empty_stock_frame()

        lookup = {as_text(column).strip().lower(): column for column in self.frame.columns}
        resolved: dict[str, str] = {}
        for target, aliases in self.COLUMN_ALIASES.items():
            for alias in aliases:
                if alias in lookup:
                    resolved[target] = lookup[alias]
                    break

        missing = [name for name in ("sku", "cod_tienda") if name not in resolved]
        if missing:
            raise ValueError(
                "El archivo de stock necesita al menos las columnas 'sku' y 'cod_tienda'. "
                f"No se encontraron: {', '.join(missing)}."
            )
        if "stock_tiendas" not in resolved:
            raise ValueError(
                "El archivo de stock necesita una columna de unidades "
                "('stock', 'stock_tiendas' o 'disponible')."
            )

        data = pd.DataFrame(
            {
                "sku": self.frame[resolved["sku"]],
                "cod_tienda": self.frame[resolved["cod_tienda"]],
                "stock_tiendas": self.frame[resolved["stock_tiendas"]],
                "stock_bodega": (
                    self.frame[resolved["stock_bodega"]] if "stock_bodega" in resolved else 0
                ),
                "fecha_corte": (
                    self.frame[resolved["fecha_corte"]] if "fecha_corte" in resolved else ""
                ),
            }
        )

        # Las columnas de reserva viajan tal cual para que `_finalize` las
        # descuente. Sin esto quedaban fuera del DataFrame que se le pasa y el
        # disponible salia sin restar lo reservado.
        for columna in self.frame.columns:
            if RESERVE_HINT in as_text(columna).strip().lower():
                data[columna] = self.frame[columna]

        wanted = {normalize_sku(sku) for sku in skus if normalize_sku(sku)}
        data["sku"] = data["sku"].map(normalize_sku)
        if wanted:
            data = data[data["sku"].isin(wanted)]

        # `_finalize` deja solo el ultimo corte y recien ahi suma las filas
        # repetidas del mismo par (SKU, tienda).
        return _finalize(
            data, self.include_central_warehouse, self.central_codes or None,
            self.central_formula,
        )


def build_stock_index(stock: pd.DataFrame) -> dict[tuple[str, str], int]:
    """Diccionario `(sku, cod_tienda) -> unidades` para acceso O(1)."""
    if stock is None or stock.empty:
        return {}
    return {
        (row.sku, row.cod_tienda): int(row.stock)
        for row in stock.itertuples(index=False)
        if int(row.stock) > 0
    }


def stock_cutoff(stock: pd.DataFrame) -> str:
    """Fecha de corte del stock cargado.

    Compara como fecha, no como texto: `31/12/2025` es mayor que `20/08/2026`
    en orden alfabetico, y esa fecha es la que se estampa en el reporte y en
    `Reasig_Fecha_Corte`.
    """
    if stock is None or stock.empty or "fecha_corte" not in stock.columns:
        return ""
    return as_text(latest_cutoff_value(stock["fecha_corte"]))


def stock_coverage(stock: pd.DataFrame, skus: Iterable[str]) -> pd.DataFrame:
    """Que paso con cada SKU consultado. Sirve para explicar un "no jala".

    Devuelve `sku | tiendas | unidades | situacion`, donde `situacion` separa
    los dos motivos que la app confundia en un solo "sin stock":

    * `SIN RESPUESTA`  : la fuente no devolvio ninguna fila para ese SKU
      (SKU inexistente en la tabla, o un cruce que no esta encontrando).
    * `EN CERO`        : la fuente si lo conoce, pero no hay unidades.
    * `CON STOCK`      : hay unidades disponibles.
    """
    pedidos = sorted({normalize_sku(sku) for sku in skus if normalize_sku(sku)})
    if stock is None or stock.empty:
        resumen: dict[str, tuple[int, int]] = {}
    else:
        agrupado = stock[stock["stock"] > 0].groupby("sku")["stock"]
        unidades = agrupado.sum().to_dict()
        tiendas = agrupado.count().to_dict()
        presentes = set(stock["sku"])
        resumen = {
            sku: (int(tiendas.get(sku, 0)), int(unidades.get(sku, 0))) for sku in presentes
        }

    filas = []
    for sku in pedidos:
        if sku not in resumen:
            situacion = "SIN RESPUESTA"
            conteo, total = 0, 0
        else:
            conteo, total = resumen[sku]
            situacion = "CON STOCK" if total > 0 else "EN CERO"
        filas.append(
            {"sku": sku, "tiendas": conteo, "unidades": total, "situacion": situacion}
        )
    return pd.DataFrame(filas, columns=["sku", "tiendas", "unidades", "situacion"])


def resolve_stock_table(secrets: dict[str, Any]) -> str:
    """Tabla de stock a consultar.

    **No se usa `table` como respaldo a proposito.** En los secrets de Catalogo
    Control Center `table` apunta a la tabla ARTI (maestro de productos), no al
    stock; ese proyecto resuelve el stock con una consulta fija contra
    `stg_pe_central_stock_bi`. Reutilizar `table` aqui haria que la app buscara
    stock en la tabla equivocada al copiar los mismos secrets.
    """
    return as_text((secrets or {}).get("stock_table")) or settings.DEFAULT_STOCK_TABLE


def secrets_to_source(
    secrets: dict[str, Any],
    include_central_warehouse: bool,
    central_codes: Iterable[str] | None = None,
    formula: Any = None,
) -> BigQueryStockSource:
    """Construye el proveedor de BigQuery desde `st.secrets`.

    Acepta el mismo bloque `[bigquery]` + `[gcp_service_account]` que usan las
    demas aplicaciones internas, para poder copiar los secrets sin editarlos.
    """
    config = dict(secrets or {})
    service_account_info = config.get("service_account_info")
    if not service_account_info and config.get("service_account_json"):
        import json

        service_account_info = json.loads(config["service_account_json"])
    return BigQueryStockSource(
        project_id=as_text(config.get("project_id")),
        job_project_id=as_text(config.get("job_project_id")),
        table=resolve_stock_table(config),
        location=as_text(config.get("location")),
        service_account_info=service_account_info,
        include_central_warehouse=include_central_warehouse,
        central_codes=tuple(sorted(central_warehouse_codes(central_codes))),
        central_formula=central_formula(formula),
        custom_query=as_text(config.get("stock_query")),
    )


def is_bigquery_configured(secrets: dict[str, Any]) -> bool:
    """Hay BigQuery utilizable si esta habilitado y hay con que autenticarse.

    La tabla no se exige: si no se declara `stock_table`, se usa la de siempre
    (`settings.DEFAULT_STOCK_TABLE`), igual que en Catalogo Control Center.
    """
    if not secrets:
        return False
    if as_text(secrets.get("enabled", "true")).lower() in ("0", "false", "no", "off"):
        return False
    # Credenciales: service account explicita, o project_id + ADC del entorno.
    return bool(
        secrets.get("service_account_info")
        or secrets.get("service_account_json")
        or as_text(secrets.get("project_id"))
    )
