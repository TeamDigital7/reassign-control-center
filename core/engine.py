"""Motor de reasignacion.

Reglas implementadas, en el orden en que se aplican a cada pedido:

1. Solo se procesan las filas cuyo `Estado` esta en `estados_objetivo`.
2. Se recorre la lista de prioridad del sitio/marca del pedido, de menor a
   mayor `prioridad`. Las bandas de prioridad traen empates, y dentro de una
   banda gana la tienda con **mas stock** (`ordenar_por_stock`).
3. Se descarta la tienda de origen del propio pedido (`excluir_tienda_origen`).
4. Se descarta cualquier tienda cuyo stock disponible, ya descontado el
   stock de seguridad y lo comprometido en esta misma corrida, no alcance
   para las unidades solicitadas.
5. Se prefiere una tienda que conserve `reserva_por_tienda` unidades despues
   de ceder. Solo si ninguna puede, se acepta dejar una tienda en cero: es el
   caso de "queda 1, mandalo igual", y queda registrado en `Reasig_Detalle`.
6. La tienda elegida cede el stock, que se descuenta en memoria de inmediato
   para que el mismo par (SKU, tienda) no se comprometa dos veces.
7. Si ninguna tienda cumple, la fila queda como `SIN OPCION DE REASIGNACION`.

El descuento es **siempre en memoria**. BigQuery no se modifica nunca.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import pandas as pd

from config import settings
from core.excel_io import (
    as_text,
    normalize_sku,
    normalize_status,
    normalize_store_code,
    normalize_store_name,
    to_units,
)
from core.priority import PriorityConfig, StoreRule

RESULT_PARTIAL = "REASIGNADO PARCIAL"

PREVIEW_COLUMNS = [
    "Pedido",
    "SKU",
    "Tienda origen",
    "Unidades",
    "Tienda reasignada",
    "Stock disponible",
    "Resultado",
]

# --- Opciones alternativas --------------------------------------------------
# Cuando la tienda elegida rechaza el pedido, quien lo gestiona necesita a
# donde mandarlo sin volver a correr la app. Se informan las siguientes
# tiendas de la lista de prioridad que **tambien** podrian cubrirlo entero.
#
# No se les reserva stock a proposito: reservarlo dejaria unidades sin usar en
# cada corrida. Por eso se muestra cuantas unidades tenia cada una al momento
# del calculo, para que la decision se tome con el dato a la vista.
MAX_ALTERNATIVES = 3
ORDINALES = {2: "2da", 3: "3ra", 4: "4ta"}


def alternative_labels(count: int) -> list[str]:
    """`['2da', '3ra', ...]` para las columnas de opciones alternativas."""
    return [ORDINALES[posicion] for posicion in range(2, 2 + max(0, count))]


def alternative_columns(count: int) -> list[str]:
    """Columnas del detalle que describen las opciones alternativas."""
    columnas: list[str] = []
    for etiqueta in alternative_labels(count):
        columnas += [f"{etiqueta} opcion", f"Stock {etiqueta} opcion"]
    return columnas


def alternative_trace_columns(count: int) -> list[str]:
    """Columnas de trazabilidad del Excel para las opciones alternativas."""
    columnas: list[str] = []
    for etiqueta in alternative_labels(count):
        columnas += [
            f"Reasig_{etiqueta}_Opcion",
            f"Reasig_Cod_{etiqueta}_Opcion",
            f"Reasig_Stock_{etiqueta}_Opcion",
        ]
    return columnas


@dataclass
class KPIs:
    pedidos_recibidos: int = 0
    pedidos_a_reasignar: int = 0
    reasignados: int = 0
    reasignados_parciales: int = 0
    sin_stock: int = 0
    errores: int = 0
    unidades_solicitadas: int = 0
    unidades_reasignadas: int = 0
    tiendas_usadas: int = 0
    ordenes_unicas: int = 0
    fecha_corte: str = ""

    @property
    def tasa_exito(self) -> float:
        if not self.pedidos_a_reasignar:
            return 0.0
        return 100.0 * (self.reasignados + self.reasignados_parciales) / self.pedidos_a_reasignar

    def to_frame(self) -> pd.DataFrame:
        # `object` a proposito: si no, la tasa de exito (float) convierte
        # todos los conteos en 402.0 en lugar de 402.
        return pd.DataFrame(
            [
                {"Indicador": "Pedidos recibidos", "Valor": self.pedidos_recibidos},
                {"Indicador": "Pedidos a reasignar", "Valor": self.pedidos_a_reasignar},
                {"Indicador": "Reasignados", "Valor": self.reasignados},
                {"Indicador": "Reasignados parciales", "Valor": self.reasignados_parciales},
                {"Indicador": "Sin stock disponible", "Valor": self.sin_stock},
                {"Indicador": "Errores", "Valor": self.errores},
                {"Indicador": "Unidades solicitadas", "Valor": self.unidades_solicitadas},
                {"Indicador": "Unidades reasignadas", "Valor": self.unidades_reasignadas},
                {"Indicador": "Tiendas utilizadas", "Valor": self.tiendas_usadas},
                {"Indicador": "Ordenes unicas", "Valor": self.ordenes_unicas},
                {"Indicador": "Tasa de exito (%)", "Valor": round(self.tasa_exito, 1)},
                {"Indicador": "Fecha de corte del stock", "Valor": self.fecha_corte or "-"},
            ],
            dtype=object,
        )


@dataclass
class ReassignmentResult:
    output_df: pd.DataFrame
    output_headers: list[str]
    preview: pd.DataFrame
    detail: pd.DataFrame
    kpis: KPIs = field(default_factory=KPIs)
    store_summary: pd.DataFrame = field(default_factory=pd.DataFrame)
    output_column: str = settings.COL_REASSIGNED


class _Ledger:
    """Stock disponible en memoria, con descuento temporal por corrida."""

    def __init__(self, stock_index: dict[tuple[str, str], int]) -> None:
        self._initial = dict(stock_index)
        self._used: dict[tuple[str, str], int] = defaultdict(int)
        self._by_store: dict[str, int] = defaultdict(int)

    def initial(self, sku: str, store: str) -> int:
        return self._initial.get((sku, store), 0)

    def available(self, sku: str, store: str, safety: int = 0) -> int:
        base = self._initial.get((sku, store), 0) - safety
        return max(0, base - self._used[(sku, store)])

    def store_used(self, store: str) -> int:
        return self._by_store[store]

    def take(self, sku: str, store: str, units: int) -> None:
        self._used[(sku, store)] += units
        self._by_store[store] += units

    def snapshot(self) -> pd.DataFrame:
        rows = [
            {
                "cod_tienda": store,
                "unidades_reasignadas": used,
            }
            for store, used in sorted(self._by_store.items(), key=lambda item: -item[1])
            if used
        ]
        return pd.DataFrame(rows)


def _origin_of(row: pd.Series, resolved: dict[str, str]) -> tuple[str, str]:
    """Devuelve `(codigo, nombre)` de la tienda que tenia el pedido."""
    code_col = resolved.get(settings.COL_STORE_CODE)
    name_col = resolved.get(settings.COL_STORE_NAME)
    code = normalize_store_code(row[code_col]) if code_col else ""
    name = as_text(row[name_col]) if name_col else ""
    return code, name


def _is_origin(rule: StoreRule, origin_code: str, origin_name: str) -> bool:
    if origin_code and rule.cod_tienda and rule.cod_tienda == origin_code:
        return True
    if origin_name and rule.nom_tienda:
        return normalize_store_name(rule.nom_tienda) == normalize_store_name(origin_name)
    return False


def _candidates(
    rules: list[StoreRule],
    sku: str,
    origin_code: str,
    origin_name: str,
    ledger: _Ledger,
    exclude_origin: bool,
    sort_by_stock: bool,
) -> tuple[list[tuple[StoreRule, int]], bool]:
    """Tiendas con stock para el SKU, ya ordenadas. Devuelve `(lista, salto_origen)`.

    Se extrajo de `_pick_store` para que la eleccion de la tienda y el calculo
    de las opciones alternativas miren exactamente la misma lista: si una
    usara otro criterio, la segunda opcion podria no ser una tienda que la
    primera pasada hubiera aceptado.
    """
    candidatas: list[tuple[StoreRule, int]] = []
    skipped_origin = False

    for rule in rules:
        if exclude_origin and _is_origin(rule, origin_code, origin_name):
            skipped_origin = True
            continue
        cap_restante = None
        if rule.max_unidades:
            cap_restante = rule.max_unidades - ledger.store_used(rule.cod_tienda)
            if cap_restante <= 0:
                continue

        available = ledger.available(sku, rule.cod_tienda, rule.stock_seguridad)
        if cap_restante is not None:
            available = min(available, cap_restante)
        if available > 0:
            candidatas.append((rule, available))

    if sort_by_stock:
        # `sorted` es estable: ante misma banda y mismo stock se conserva el
        # orden original de la lista de prioridad.
        candidatas.sort(key=lambda item: (item[0].prioridad, -item[1]))
    return candidatas, skipped_origin


def alternative_stores(
    rules: list[StoreRule],
    sku: str,
    units: int,
    origin_code: str,
    origin_name: str,
    ledger: _Ledger,
    exclude_origin: bool,
    sort_by_stock: bool,
    chosen: StoreRule | None,
    count: int,
) -> list[tuple[StoreRule, int]]:
    """Siguientes tiendas que podrian cubrir el mismo pedido entero.

    Solo entran las que cubren las `units` completas: una tienda que cubre la
    mitad no sirve como plan B para quien esta gestionando el rechazo.

    Se calcula **antes** de que la tienda elegida tome su stock, pero eso no
    altera el resultado: la elegida se excluye y el descuento no toca a las
    demas. Lo que si refleja es lo ya comprometido por los pedidos anteriores
    de la misma corrida.
    """
    if count <= 0 or chosen is None or not rules:
        return []
    candidatas, _ = _candidates(
        rules, sku, origin_code, origin_name, ledger, exclude_origin, sort_by_stock
    )
    alternativas: list[tuple[StoreRule, int]] = []
    for rule, available in candidatas:
        if len(alternativas) >= count:
            break
        if rule.cod_tienda and rule.cod_tienda == chosen.cod_tienda:
            continue
        if available >= units:
            alternativas.append((rule, available))
    return alternativas


def _pick_store(
    rules: list[StoreRule],
    sku: str,
    units: int,
    origin_code: str,
    origin_name: str,
    ledger: _Ledger,
    exclude_origin: bool,
    allow_partial: bool,
    reserva: int = 0,
    sort_by_stock: bool = True,
) -> tuple[StoreRule | None, int, int, str]:
    """Elige tienda destino. Devuelve `(regla, disponible, a_tomar, motivo)`.

    La eleccion respeta la prioridad del area comercial y, dentro de ella,
    cuida el stock de la tienda que cede la unidad:

    1. Se ordenan las candidatas por banda de prioridad. Como las bandas traen
       empates (la lista real tiene 26 tiendas en la banda 11), dentro de cada
       banda gana la que tiene **mas stock**, no la primera alfabeticamente.
    2. Primera pasada: solo tiendas que quedarian con al menos `reserva`
       unidades despues de ceder. Asi no se vacia una tienda que tiene poco.
    3. Segunda pasada: si ninguna cumple, recien ahi se acepta dejar la tienda
       en cero. Es el caso de "solo queda 1, mandalo igual".
    """
    if not rules:
        return None, 0, 0, "El sitio/marca no tiene tiendas en la lista de prioridad."

    candidatas, skipped_origin = _candidates(
        rules, sku, origin_code, origin_name, ledger, exclude_origin, sort_by_stock
    )

    if not candidatas:
        reason = (
            f"Ninguna de las {len(rules)} tiendas de la prioridad tiene stock de este SKU "
            "en el corte consultado."
        )
        if skipped_origin:
            reason += " Se descarto la tienda de origen."
        return None, 0, 0, reason

    # Pasada 1: la tienda conserva `reserva` unidades despues de ceder.
    if reserva > 0:
        for rule, available in candidatas:
            if available - units >= reserva:
                return rule, available, units, ""

    # Pasada 2: alcanza justo, aunque la tienda quede en cero.
    for rule, available in candidatas:
        if available >= units:
            motivo = ""
            if reserva > 0:
                restante = available - units
                motivo = (
                    f"Ultimo recurso: ninguna tienda conservaba {reserva} unidad(es) de reserva; "
                    f"esta queda en {restante}."
                )
            return rule, available, units, motivo

    if allow_partial:
        rule, available = max(candidatas, key=lambda item: item[1])
        return rule, available, available, f"Solo se cubren {available} de {units} unidades."

    # Aqui SI habia stock, solo que ninguna tienda reunia las unidades pedidas.
    # Informar el mejor disponible en vez de 0 es lo que permite distinguir
    # "no hay stock en ningun lado" de "hay, pero repartido y no alcanza": con
    # un 0 en ambos casos, desde la app las dos situaciones se ven identicas.
    mejor_regla, mejor = max(candidatas, key=lambda item: item[1])
    reason = (
        f"Ninguna tienda reune las {units} unidades requeridas; la mejor es "
        f"{mejor_regla.nom_tienda or mejor_regla.cod_tienda} con {mejor}."
    )
    if skipped_origin:
        reason += " Se descarto la tienda de origen."
    return None, mejor, 0, reason


def reassign(
    df: pd.DataFrame,
    headers: list[str],
    resolved: dict[str, str],
    config: PriorityConfig,
    stock_index: dict[tuple[str, str], int],
    stock_cutoff: str = "",
    include_trace: bool = True,
) -> ReassignmentResult:
    """Ejecuta la reasignacion completa y arma el archivo de salida."""
    output_column = config.output_column
    exclude_origin = config.flag("excluir_tienda_origen")
    allow_partial = config.flag("permitir_reasignacion_parcial")
    group_by_shgroup = config.flag("agrupar_por_shgroup")
    group_fallback = config.flag("fallback_linea_si_grupo_falla")
    reserva = max(0, config.number("reserva_por_tienda", 1))
    sort_by_stock = config.flag("ordenar_por_stock")
    n_alternativas = min(MAX_ALTERNATIVES, max(0, config.number("opciones_alternativas", 1)))
    cols_alternativas = alternative_columns(n_alternativas)
    trace_alternativas = alternative_trace_columns(n_alternativas)
    target_statuses = set(config.target_statuses)

    col_status = resolved[settings.COL_STATUS]
    col_sku = resolved[settings.COL_SKU]
    col_units = resolved[settings.COL_UNITS]
    col_order = resolved.get(settings.COL_ORDER)
    col_site = resolved.get(settings.COL_SITE)
    col_brand = resolved.get(settings.COL_BRAND)
    col_shgroup = resolved.get(settings.COL_SHGROUP)

    ledger = _Ledger(stock_index)
    output = df.copy()

    # Columnas nuevas: la de salida (si no existia) y las de trazabilidad.
    # Se crean como `object` a proposito: pandas 3 infiere `str` en una columna
    # inicializada con "" y luego rechaza los enteros de stock y prioridad.
    def _blank_column() -> pd.Series:
        return pd.Series([""] * len(output), index=output.index, dtype=object)

    output_headers = list(headers)
    if output_column not in output.columns:
        output[output_column] = _blank_column()
        output_headers.append(output_column)
    else:
        output[output_column] = output[output_column].map(as_text).astype(object)

    for column in settings.TRACE_COLUMNS + trace_alternativas:
        output[column] = _blank_column()
        if include_trace and column not in output_headers:
            output_headers.append(column)

    kpis = KPIs(pedidos_recibidos=len(df), fecha_corte=stock_cutoff)
    if col_order:
        kpis.ordenes_unicas = int(df[col_order].map(as_text).replace("", pd.NA).nunique())

    statuses = df[col_status].map(normalize_status)
    target_index = list(df.index[statuses.isin(target_statuses)])
    kpis.pedidos_a_reasignar = len(target_index)

    detail_rows: list[dict] = []

    # --- modo agrupado: se intenta una sola tienda por ShGroup -------------
    group_assignment: dict[str, StoreRule] = {}
    group_failed: set[str] = set()
    if group_by_shgroup and col_shgroup:
        groups: dict[str, list] = defaultdict(list)
        for index in target_index:
            groups[as_text(df.at[index, col_shgroup])].append(index)

        for group_key, indexes in groups.items():
            if not group_key or len(indexes) < 2:
                continue
            first = df.loc[indexes[0]]
            site = as_text(first[col_site]) if col_site else ""
            brand = as_text(first[col_brand]) if col_brand else ""
            origin_code, origin_name = _origin_of(first, resolved)
            rules = config.rules_for(site, brand)

            needs: dict[str, int] = defaultdict(int)
            for index in indexes:
                sku = normalize_sku(df.at[index, col_sku])
                if sku:
                    needs[sku] += max(1, to_units(df.at[index, col_units]))

            chosen = None
            for rule in rules:
                if exclude_origin and _is_origin(rule, origin_code, origin_name):
                    continue
                if all(
                    ledger.available(sku, rule.cod_tienda, rule.stock_seguridad) - units >= reserva
                    for sku, units in needs.items()
                ):
                    chosen = rule
                    break

            if chosen is not None:
                for index in indexes:
                    group_assignment[str(index)] = chosen
            else:
                group_failed.update(str(index) for index in indexes)

    # --- recorrido principal, en el orden original del archivo -------------
    for index in target_index:
        row = df.loc[index]
        sku = normalize_sku(row[col_sku])
        units = to_units(row[col_units])
        if units <= 0:
            units = 1
        kpis.unidades_solicitadas += units

        order_id = as_text(row[col_order]) if col_order else ""
        site = as_text(row[col_site]) if col_site else ""
        brand = as_text(row[col_brand]) if col_brand else ""
        origin_code, origin_name = _origin_of(row, resolved)
        origin_label = origin_name or (config.store_name(origin_code) if origin_code else "")

        record = {
            "Pedido": order_id,
            "SKU": sku,
            "Tienda origen": origin_label or "(sin dato)",
            "Unidades": units,
            "Tienda reasignada": "",
            "Stock disponible": 0,
            "Resultado": "",
            "Sitio": site,
            "Marca": brand,
            "Cod tienda reasignada": "",
            "Prioridad": "",
            "Stock restante": "",
            "Detalle": "",
        }
        for columna in cols_alternativas:
            record[columna] = ""

        if not sku:
            record["Resultado"] = settings.RESULT_ERROR
            record["Detalle"] = "La fila no trae SKU: no se puede consultar stock."
            kpis.errores += 1
            output.at[index, "Reasig_Resultado"] = settings.RESULT_ERROR
            output.at[index, "Reasig_Detalle"] = record["Detalle"]
            detail_rows.append(record)
            continue

        key = str(index)
        forced = group_assignment.get(key)

        if forced is not None:
            available = ledger.available(sku, forced.cod_tienda, forced.stock_seguridad)
            rule, take, reason = forced, min(units, available), ""
            if available < units:
                # No deberia ocurrir, pero si el grupo se descuadra se degrada
                # a la busqueda normal en lugar de asignar de mas.
                rule, available, take, reason = _pick_store(
                    config.rules_for(site, brand), sku, units, origin_code, origin_name,
                    ledger, exclude_origin, allow_partial, reserva, sort_by_stock,
                )
        elif key in group_failed and not group_fallback:
            rule, available, take, reason = (
                None,
                0,
                0,
                "Ninguna tienda cubre el despacho completo (agrupar_por_shgroup=SI).",
            )
        else:
            rule, available, take, reason = _pick_store(
                config.rules_for(site, brand), sku, units, origin_code, origin_name,
                ledger, exclude_origin, allow_partial, reserva, sort_by_stock,
            )
            if key in group_failed and rule is not None:
                reason = (reason + " ").strip() + " Despacho dividido: ninguna tienda cubria el ShGroup completo."

        if rule is None:
            record["Resultado"] = settings.RESULT_NO_OPTION
            record["Stock disponible"] = available
            record["Detalle"] = reason
            kpis.sin_stock += 1
            output.at[index, output_column] = ""
            output.at[index, "Reasig_Resultado"] = settings.RESULT_NO_OPTION
            output.at[index, "Reasig_Stock_Disponible"] = available
            output.at[index, "Reasig_Detalle"] = reason
            output.at[index, "Reasig_Fecha_Corte"] = stock_cutoff
            detail_rows.append(record)
            continue

        # Antes de tomar el stock: la elegida se excluye igual, y asi las
        # unidades informadas son las que habia al momento de decidir.
        alternativas = alternative_stores(
            config.rules_for(site, brand), sku, units, origin_code, origin_name,
            ledger, exclude_origin, sort_by_stock, rule, n_alternativas,
        )
        for posicion, etiqueta in enumerate(alternative_labels(n_alternativas)):
            if posicion >= len(alternativas):
                break
            alterna, disponible = alternativas[posicion]
            record[f"{etiqueta} opcion"] = alterna.nom_tienda or alterna.cod_tienda
            record[f"Stock {etiqueta} opcion"] = disponible
            output.at[index, f"Reasig_{etiqueta}_Opcion"] = alterna.nom_tienda
            output.at[index, f"Reasig_Cod_{etiqueta}_Opcion"] = alterna.cod_tienda
            output.at[index, f"Reasig_Stock_{etiqueta}_Opcion"] = disponible

        ledger.take(sku, rule.cod_tienda, take)
        remaining = ledger.available(sku, rule.cod_tienda, rule.stock_seguridad)
        partial = take < units
        result = RESULT_PARTIAL if partial else settings.RESULT_REASSIGNED
        if partial:
            kpis.reasignados_parciales += 1
        else:
            kpis.reasignados += 1
        kpis.unidades_reasignadas += take

        record.update(
            {
                "Tienda reasignada": rule.nom_tienda,
                "Stock disponible": available,
                "Resultado": result,
                "Cod tienda reasignada": rule.cod_tienda,
                "Prioridad": rule.prioridad,
                "Stock restante": remaining,
                "Detalle": reason,
            }
        )
        detail_rows.append(record)

        output.at[index, output_column] = rule.nom_tienda
        output.at[index, "Reasig_Resultado"] = result
        output.at[index, "Reasig_Cod_Tienda"] = rule.cod_tienda
        output.at[index, "Reasig_Stock_Disponible"] = available
        output.at[index, "Reasig_Stock_Restante"] = remaining
        output.at[index, "Reasig_Prioridad"] = rule.prioridad
        output.at[index, "Reasig_Detalle"] = reason
        output.at[index, "Reasig_Fecha_Corte"] = stock_cutoff

    # Filas fuera de alcance: se marcan pero no se tocan.
    for index in df.index.difference(target_index):
        output.at[index, "Reasig_Resultado"] = settings.RESULT_NOT_APPLICABLE

    detail = pd.DataFrame(detail_rows) if detail_rows else pd.DataFrame(columns=PREVIEW_COLUMNS)
    columnas_preview = PREVIEW_COLUMNS + [c for c in cols_alternativas if c in detail.columns]
    preview = detail[columnas_preview] if not detail.empty else detail

    store_summary = ledger.snapshot()
    if not store_summary.empty:
        store_summary["nom_tienda"] = store_summary["cod_tienda"].map(config.store_name)
        store_summary = store_summary[["cod_tienda", "nom_tienda", "unidades_reasignadas"]]
        kpis.tiendas_usadas = len(store_summary)

    if not include_trace:
        descartables = set(settings.TRACE_COLUMNS) | set(trace_alternativas)
        output_headers = [h for h in output_headers if h not in descartables]

    return ReassignmentResult(
        output_df=output,
        output_headers=output_headers,
        preview=preview,
        detail=detail,
        kpis=kpis,
        store_summary=store_summary,
        output_column=output_column,
    )


def verify_result(
    result: "ReassignmentResult",
    stock_index: dict[tuple[str, str], int],
    config: PriorityConfig,
    original_headers: list[str],
) -> list[str]:
    """Revisa el resultado antes de generar el Excel. Devuelve los problemas.

    Es una auditoria independiente del motor: recalcula el consumo de stock
    desde cero sobre el detalle producido, en vez de confiar en el ledger que
    uso el propio motor. Si ambos no coinciden, algo esta mal y hay que verlo
    antes de que el archivo salga a la plataforma.
    """
    problemas: list[str] = []
    detail = result.detail
    if detail.empty:
        return ["La corrida no produjo ningun pedido procesado."]

    validos = set(config.target_statuses)

    # 1. Solo se tocaron estados validos.
    fuera = result.output_df[
        (result.output_df["Reasig_Resultado"] == settings.RESULT_NOT_APPLICABLE)
        & (result.output_df[result.output_column].map(as_text) != "")
    ]
    if len(fuera):
        problemas.append(
            f"{len(fuera)} filas fuera de los estados {sorted(validos)} recibieron tienda."
        )

    # 2 y 4. Reconteo independiente: nadie usa stock ya consumido ni de mas.
    consumo: dict[tuple[str, str], int] = defaultdict(int)
    for _, row in detail.iterrows():
        code = as_text(row["Cod tienda reasignada"])
        if code:
            consumo[(row["SKU"], code)] += int(row["Unidades"])
    for (sku, code), usado in sorted(consumo.items()):
        disponible = stock_index.get((sku, code), 0)
        if usado > disponible:
            problemas.append(
                f"SKU {sku} en tienda {code}: se repartieron {usado} unidades "
                f"y BigQuery reporto {disponible}."
            )

    # 3. La tienda asignada esta en la lista de prioridad del sitio del pedido.
    for _, row in detail.iterrows():
        code = as_text(row["Cod tienda reasignada"])
        if not code:
            continue
        permitidas = {rule.cod_tienda for rule in config.rules_for(row["Sitio"], row["Marca"])}
        if code not in permitidas:
            problemas.append(
                f"Pedido {row['Pedido']}: la tienda {code} no esta en la prioridad "
                f"del sitio '{row['Sitio']}'."
            )
            break  # con un caso basta para revisar la configuracion

    # 3b. Nunca la tienda de origen.
    if config.flag("excluir_tienda_origen"):
        for _, row in detail.iterrows():
            origen = normalize_store_name(row["Tienda origen"])
            destino = normalize_store_name(row["Tienda reasignada"])
            if origen and destino and origen == destino:
                problemas.append(f"Pedido {row['Pedido']} se reasigno a su propia tienda origen.")
                break

    # 5. Ninguna columna original se perdio.
    faltantes = [h for h in original_headers if h not in result.output_df.columns]
    if faltantes:
        problemas.append(f"Se perdieron columnas del archivo original: {faltantes}.")

    # 6. La columna de salida existe y coincide con el detalle.
    if result.output_column not in result.output_headers:
        problemas.append(f"Falta la columna '{result.output_column}' en el archivo de salida.")
    else:
        escritas = int((result.output_df[result.output_column].map(as_text) != "").sum())
        esperadas = int((detail["Tienda reasignada"].map(as_text) != "").sum())
        if escritas != esperadas:
            problemas.append(
                f"'{result.output_column}' tiene {escritas} valores pero se reasignaron "
                f"{esperadas} pedidos."
            )

    # 7. Los sin opcion quedan marcados y sin tienda.
    sin_opcion = detail[detail["Resultado"] == settings.RESULT_NO_OPTION]
    con_tienda = sin_opcion[sin_opcion["Tienda reasignada"].map(as_text) != ""]
    if len(con_tienda):
        problemas.append(f"{len(con_tienda)} pedidos sin opcion tienen tienda asignada.")

    # 8. Los KPI cuadran con el detalle.
    kpis = result.kpis
    suma = kpis.reasignados + kpis.reasignados_parciales + kpis.sin_stock + kpis.errores
    if suma != kpis.pedidos_a_reasignar:
        problemas.append(
            f"Los indicadores no cuadran: {suma} clasificados vs "
            f"{kpis.pedidos_a_reasignar} pedidos validos."
        )

    return problemas


def target_skus(df: pd.DataFrame, resolved: dict[str, str], config: PriorityConfig) -> list[str]:
    """SKU unicos que hay que consultar en la fuente de stock."""
    col_status = resolved[settings.COL_STATUS]
    col_sku = resolved[settings.COL_SKU]
    statuses = df[col_status].map(normalize_status)
    targets = set(config.target_statuses)
    skus = df.loc[statuses.isin(targets), col_sku].map(normalize_sku)
    return sorted({sku for sku in skus if sku})
