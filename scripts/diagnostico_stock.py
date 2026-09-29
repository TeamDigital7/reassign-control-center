"""Diagnostico de la fuente de stock. Solo lectura, contra la tabla real.

    python -m scripts.diagnostico_stock
    python -m scripts.diagnostico_stock "Formato de Carga Reasignacion.xls"

Cuando la app dice "sin stock" hay media docena de causas que desde la
interfaz se ven exactamente igual. Este script las separa una por una, con
datos de la tabla en vez de suposiciones, y en el mismo orden en que el stock
se puede perder:

    conexion -> esquema -> cortes -> cruce de SKU -> unidades -> tiendas

Lee las credenciales de `.streamlit/secrets.toml`, el mismo archivo que usa
la app. No escribe nada: son todas consultas de lectura.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from config import settings
from core.excel_io import as_text, normalize_sku, normalize_store_code
from core.priority import load_priority
from core.stock_source import (
    DIA_CORTE_SQL,
    SKU_CANONICO_SQL,
    SKU_LIMPIO_SQL,
    resolve_stock_table,
    secrets_to_source,
)

SECRETS_FILE = settings.BASE_DIR / ".streamlit" / "secrets.toml"

# Columnas cuyo TIPO decide si el cruce puede funcionar. Un `id_producto`
# FLOAT64 o un `fecha_corte` TIMESTAMP cambian por completo el resultado.
COLUMNAS_CLAVE = ("id_producto", "codigo_tienda", "fecha_corte", "stock_tiendas", "stock_bodega")


def titulo(texto: str) -> None:
    print(f"\n{'=' * 72}\n{texto}\n{'=' * 72}")


def leer_secrets() -> dict[str, Any]:
    if not SECRETS_FILE.exists():
        print(f"No existe {SECRETS_FILE}.")
        print("Copia .streamlit/secrets.example.toml a .streamlit/secrets.toml y completalo.")
        raise SystemExit(2)
    import tomllib

    with SECRETS_FILE.open("rb") as handle:
        datos = tomllib.load(handle)
    config = dict(datos.get("bigquery", {}))
    cuenta = datos.get("gcp_service_account")
    if cuenta:
        config["service_account_info"] = dict(cuenta)
    return config


def skus_del_archivo(ruta: Path) -> list[str]:
    """SKU de los estados objetivo, tal como los mandaria la app."""
    from core import engine, excel_io
    from core.validation import resolve_columns

    payload = excel_io.read_orders(ruta.read_bytes(), ruta.name)
    config = load_priority()
    resolved = resolve_columns(payload.headers)
    if settings.COL_SKU not in resolved or settings.COL_STATUS not in resolved:
        print("  El archivo no tiene columnas de SKU y estado reconocibles.")
        return []
    skus = engine.target_skus(payload.df, resolved, config)
    print(f"  {ruta.name}: {payload.n_rows} filas, {len(skus)} SKU en estados "
          f"{', '.join(config.target_statuses)}.")
    return skus


def paso_esquema(client, tabla: str) -> dict[str, str]:
    titulo("2. Esquema de la tabla  (el TIPO decide si el cruce puede funcionar)")
    info = client.get_table(tabla)
    tipos = {campo.name: campo.field_type for campo in info.schema}
    print(f"  {info.full_table_id}: {info.num_rows:,} filas.")
    faltantes = [c for c in COLUMNAS_CLAVE if c not in tipos]
    for columna in COLUMNAS_CLAVE:
        tipo = tipos.get(columna, "NO EXISTE")
        nota = ""
        if columna == "id_producto" and tipo in ("FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC"):
            nota = "  <-- numerico: su texto es '5438957.0', por eso se canoniza"
        if columna == "fecha_corte" and tipo in ("TIMESTAMP", "DATETIME"):
            nota = "  <-- lleva hora: el corte DEBE compararse por dia, no por instante"
        if columna in ("stock_tiendas", "stock_bodega") and tipo == "STRING":
            nota = "  <-- texto: un CAST directo a INT64 falla"
        print(f"    {columna:<16} {tipo}{nota}")
    if faltantes:
        print(f"\n  FALTAN columnas que la consulta usa: {', '.join(faltantes)}.")
        print("  La consulta por defecto no puede funcionar contra esta tabla.")
    return tipos


def paso_cortes(client, tabla: str, location: str) -> str:
    titulo("3. Cortes disponibles  (el stock es una foto: solo entra el ultimo dia)")
    dia = DIA_CORTE_SQL.format(column="s.fecha_corte")
    sql = f"""
    SELECT
      CAST({dia} AS STRING) AS dia,
      COUNT(*)              AS filas,
      COUNT(DISTINCT CAST(s.id_producto AS STRING)) AS skus,
      COUNT(DISTINCT CAST(s.codigo_tienda AS STRING)) AS tiendas
    FROM `{tabla}` AS s
    GROUP BY dia
    ORDER BY dia DESC
    LIMIT 8
    """
    filas = list(client.query(sql, location=location or None).result())
    if not filas:
        print("  La tabla no devolvio ningun corte: esta vacia.")
        return ""
    print(f"    {'dia':<14}{'filas':>12}{'SKU':>10}{'tiendas':>10}")
    for fila in filas:
        print(f"    {str(fila.dia or '(ilegible)'):<14}{fila.filas:>12,}{fila.skus:>10,}{fila.tiendas:>10,}")
    ultimo = filas[0]
    if ultimo.dia is None:
        print("\n  El dia mas reciente no se pudo leer como fecha: revisa el formato de fecha_corte.")
    elif ultimo.filas < (max(f.filas for f in filas) / 10):
        print(f"\n  OJO: el corte mas reciente ({ultimo.dia}) tiene MUCHAS menos filas que los")
        print("  anteriores. Parece una carga a medio terminar; la app solo usa ese dia.")
    return as_text(ultimo.dia)


def paso_cruce(client, tabla: str, location: str, skus: list[str]) -> None:
    titulo("4. Cruce de SKU  (donde se pierde el stock, paso a paso)")
    if not skus:
        print("  Sin archivo de pedidos no hay SKU que cruzar.")
        print("  Vuelve a correr esto pasando el Excel:")
        print("      python -m scripts.diagnostico_stock \"Formato de Carga Reasignacion.xls\"")
        return

    from google.cloud import bigquery

    muestra = skus[:1000]
    limpio = SKU_LIMPIO_SQL.format(column="s.id_producto")
    canonico = SKU_CANONICO_SQL.format(column=limpio)
    dia = DIA_CORTE_SQL.format(column="s.fecha_corte")
    sql = f"""
    WITH con_dia AS (
      SELECT
        {canonico}                    AS sku,
        CAST(s.id_producto AS STRING) AS sku_crudo,
        {dia}                         AS dia_corte,
        COALESCE(SAFE_CAST(CAST(s.stock_tiendas AS STRING) AS FLOAT64), 0) AS unidades
      FROM `{tabla}` AS s
    ),
    corte AS (SELECT MAX(dia_corte) AS dia FROM con_dia)
    SELECT
      (SELECT COUNT(DISTINCT sku) FROM con_dia WHERE sku IN UNNEST(@skus)) AS en_tabla,
      (SELECT COUNT(DISTINCT sku_crudo) FROM con_dia WHERE sku_crudo IN UNNEST(@skus)) AS crudo,
      (SELECT COUNT(DISTINCT n.sku) FROM con_dia n, corte c
         WHERE n.dia_corte = c.dia AND n.sku IN UNNEST(@skus)) AS en_corte,
      (SELECT COUNT(DISTINCT n.sku) FROM con_dia n, corte c
         WHERE n.dia_corte = c.dia AND n.sku IN UNNEST(@skus) AND n.unidades > 0) AS con_unidades
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("skus", "STRING", muestra)]
    )
    r = next(iter(client.query(sql, job_config=job_config, location=location or None).result()))

    print(f"    SKU consultados (muestra)      {len(muestra):>8,}")
    print(f"    ...que existen en la tabla     {r.en_tabla:>8,}")
    print(f"    ...que estan en el ultimo corte{r.en_corte:>8,}")
    print(f"    ...con al menos 1 unidad       {r.con_unidades:>8,}")

    if not r.en_tabla and r.crudo:
        print("\n  CAUSA: el SKU cruza contra el valor CRUDO pero no contra el canonizado.")
        print("  La canonizacion esta de mas para esta tabla. Avisa al equipo.")
    elif not r.en_tabla:
        print("\n  CAUSA: ningun SKU del archivo existe en la tabla, ni crudo ni canonizado.")
        print("  La tabla usa otro maestro de codigos. Ejemplos de la tabla:")
        ejemplos = client.query(
            f"SELECT DISTINCT CAST(s.id_producto AS STRING) AS v FROM `{tabla}` AS s LIMIT 5",
            location=location or None,
        ).result()
        print("    tabla :", ", ".join(as_text(f.v) for f in ejemplos))
        print("    excel :", ", ".join(muestra[:5]))
    elif not r.en_corte:
        print("\n  CAUSA: los SKU existen, pero solo en cortes anteriores.")
        print("  El corte vigente no los trae y el stock no usa fechas viejas.")
    elif not r.con_unidades:
        print("\n  CAUSA: los SKU estan en el corte vigente, pero todos en cero.")
        print("  No es un problema de la app: no hay stock que reasignar.")
    else:
        print(f"\n  El cruce funciona: {r.con_unidades:,} SKU con unidades en el corte vigente.")
        print("  Si aun asi la app no reasigna, el problema esta en las tiendas (paso 5).")


def paso_tiendas(client, tabla: str, location: str) -> None:
    titulo("5. Tiendas  (tener stock no basta: la tienda debe estar en la prioridad)")
    dia = DIA_CORTE_SQL.format(column="s.fecha_corte")
    sql = f"""
    WITH con_dia AS (
      SELECT CAST(s.codigo_tienda AS STRING) AS cod_tienda, {dia} AS dia_corte
      FROM `{tabla}` AS s
    ),
    corte AS (SELECT MAX(dia_corte) AS dia FROM con_dia)
    SELECT DISTINCT n.cod_tienda
    FROM con_dia n, corte c
    WHERE n.dia_corte = c.dia
    """
    de_bigquery = {
        normalize_store_code(f.cod_tienda)
        for f in client.query(sql, location=location or None).result()
    }
    config = load_priority()
    de_prioridad = {normalize_store_code(regla["cod_tienda"]) for regla in config.rules}
    de_prioridad.discard("")
    comunes = de_bigquery & de_prioridad

    print(f"    Tiendas en el corte vigente    {len(de_bigquery):>8,}")
    print(f"    Tiendas en la prioridad        {len(de_prioridad):>8,}")
    print(f"    En ambas (las unicas usables)  {len(comunes):>8,}")
    if not comunes:
        print("\n  CAUSA: ninguna tienda de la prioridad aparece en el stock.")
        print("  Los codigos no son del mismo tipo. Ejemplos:")
        print("    BigQuery :", ", ".join(sorted(de_bigquery)[:8]))
        print("    prioridad:", ", ".join(sorted(de_prioridad)[:8]))
    else:
        sin_stock = sorted(de_prioridad - de_bigquery)
        if sin_stock:
            print(f"\n  {len(sin_stock)} tiendas de la prioridad no estan en el corte: "
                  f"{', '.join(sin_stock[:10])}")


def main(argv: list[str]) -> int:
    print("Diagnostico de la fuente de stock — solo lectura")
    secrets = leer_secrets()

    titulo("1. Conexion")
    tabla = resolve_stock_table(secrets)
    print(f"  Tabla de stock: {tabla}")
    if as_text(secrets.get("stock_query")):
        print("  OJO: hay un `stock_query` propio en los secrets. La app usa ESA consulta,")
        print("  no la de la app, asi que estos numeros pueden no coincidir con lo que ves.")
    source = secrets_to_source(secrets, True)
    ok, detalle = source.test_connection()
    print(f"  {'OK' if ok else 'FALLO'}: {detalle}")
    if not ok:
        return 1

    client = source._client()
    location = source.location

    paso_esquema(client, tabla)
    paso_cortes(client, tabla, location)

    skus: list[str] = []
    if len(argv) > 1:
        ruta = Path(argv[1])
        if ruta.exists():
            print()
            skus = skus_del_archivo(ruta)
        else:
            print(f"\n  No se encontro el archivo {ruta}.")
    paso_cruce(client, tabla, location, skus)
    paso_tiendas(client, tabla, location)

    titulo("Fin")
    print("  Copia esta salida completa para que podamos leer el resultado.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
