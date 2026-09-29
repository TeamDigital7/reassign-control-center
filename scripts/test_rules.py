"""Pruebas de las reglas de negocio del motor, con escenarios controlados.

    python -m scripts.test_rules

Cada caso arma un mini archivo de pedidos y un stock a medida, para que el
resultado esperado sea evidente sin depender de BigQuery ni del Excel real.
"""

from __future__ import annotations

import sys

import pandas as pd

from config import settings
from core import engine
from core.priority import PriorityConfig
from core.validation import resolve_columns

HEADERS = [
    settings.COL_ORDER,
    settings.COL_SHGROUP,
    settings.COL_SITE,
    settings.COL_STATUS,
    settings.COL_STORE_NAME,
    settings.COL_STORE_CODE,
    settings.COL_BRAND,
    settings.COL_SKU,
    settings.COL_UNITS,
]

STORES = {
    "10": "TIENDA A",
    "20": "TIENDA B",
    "30": "TIENDA C",
}


def make_config(**params) -> PriorityConfig:
    config = PriorityConfig(params={**settings.DEFAULT_PARAMS, **params})
    config.stores = {
        code: {"cod_tienda": code, "nom_tienda": name, "activo": True, "stock_seguridad": 0}
        for code, name in STORES.items()
    }
    config.rules = [
        {
            "sitio": "*",
            "marca": "*",
            "cod_tienda": code,
            "nom_tienda": name,
            "prioridad": position,
            "activo": True,
            "stock_seguridad": 0,
            "max_unidades": 0,
        }
        for position, (code, name) in enumerate(STORES.items(), start=1)
    ]
    return config


def make_orders(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=HEADERS, dtype=object)


def run(orders: pd.DataFrame, stock: dict, config: PriorityConfig):
    return engine.reassign(
        df=orders,
        headers=HEADERS,
        resolved=resolve_columns(HEADERS),
        config=config,
        stock_index=stock,
        stock_cutoff="2026-08-17",
        include_trace=True,
    )


def order(order_id: str, sku: str, units: int, store_name: str = "", store_code: str = "", **extra):
    row = {
        settings.COL_ORDER: order_id,
        settings.COL_SHGROUP: extra.get("shgroup", ""),
        settings.COL_SITE: extra.get("site", "columbiaperu"),
        settings.COL_STATUS: extra.get("status", "SIN_STOCK"),
        settings.COL_STORE_NAME: store_name,
        settings.COL_STORE_CODE: store_code,
        settings.COL_BRAND: extra.get("brand", "Columbia"),
        settings.COL_SKU: sku,
        settings.COL_UNITS: units,
    }
    return row


CASES: list[tuple[str, callable]] = []


def case(name: str):
    def decorator(fn):
        CASES.append((name, fn))
        return fn

    return decorator


# ---------------------------------------------------------------------------
@case("Regla 5: gana la tienda de mayor prioridad, no la de mas stock")
def test_priority_wins():
    orders = make_orders([order("P1", "S1", 1)])
    # TIENDA A tiene poco frente a B, pero suficiente para no quedar vacia:
    # la banda de prioridad manda. (Con 1 sola unidad ganaria B, ver
    # "Stock: no se vacia una tienda...").
    stock = {("S1", "10"): 3, ("S1", "20"): 99}
    result = run(orders, stock, make_config())
    assert result.detail.loc[0, "Tienda reasignada"] == "TIENDA A", result.detail.loc[0].to_dict()


@case("Regla 6: el stock se descuenta durante la misma corrida")
def test_stock_decrement():
    orders = make_orders([order("P1", "S1", 1), order("P2", "S1", 1), order("P3", "S1", 1)])
    stock = {("S1", "10"): 2, ("S1", "20"): 1}
    result = run(orders, stock, make_config())
    assigned = list(result.detail["Tienda reasignada"])
    assert assigned == ["TIENDA A", "TIENDA A", "TIENDA B"], assigned


@case("Regla 6: no se reparte mas stock del que existe")
def test_no_oversell():
    orders = make_orders([order(f"P{i}", "S1", 1) for i in range(5)])
    stock = {("S1", "10"): 2, ("S1", "20"): 1}
    result = run(orders, stock, make_config())
    assert result.kpis.unidades_reasignadas == 3, result.kpis.unidades_reasignadas
    assert result.kpis.sin_stock == 2, result.kpis.sin_stock


@case("Regla 6: unidades multiples requieren cobertura completa")
def test_units_must_fit():
    orders = make_orders([order("P1", "S1", 3)])
    stock = {("S1", "10"): 2, ("S1", "20"): 5}
    result = run(orders, stock, make_config())
    assert result.detail.loc[0, "Tienda reasignada"] == "TIENDA B", result.detail.loc[0].to_dict()


@case("Regla 7: nunca se reasigna a la tienda de origen (por codigo)")
def test_exclude_origin_by_code():
    orders = make_orders([order("P1", "S1", 1, store_code="10")])
    stock = {("S1", "10"): 5, ("S1", "20"): 5}
    result = run(orders, stock, make_config())
    assert result.detail.loc[0, "Tienda reasignada"] == "TIENDA B", result.detail.loc[0].to_dict()


@case("Regla 7: tambien se excluye por nombre cuando no viene el codigo")
def test_exclude_origin_by_name():
    orders = make_orders([order("P1", "S1", 1, store_name="tienda a")])
    stock = {("S1", "10"): 5, ("S1", "20"): 5}
    result = run(orders, stock, make_config())
    assert result.detail.loc[0, "Tienda reasignada"] == "TIENDA B", result.detail.loc[0].to_dict()


@case("Regla 7: se puede desactivar por configuracion")
def test_origin_rule_off():
    orders = make_orders([order("P1", "S1", 1, store_code="10")])
    stock = {("S1", "10"): 5, ("S1", "20"): 5}
    result = run(orders, stock, make_config(excluir_tienda_origen="NO"))
    assert result.detail.loc[0, "Tienda reasignada"] == "TIENDA A", result.detail.loc[0].to_dict()


@case("Regla 8: sin stock en ninguna tienda -> SIN OPCION DE REASIGNACION")
def test_no_option():
    orders = make_orders([order("P1", "S9", 1)])
    result = run(orders, {}, make_config())
    assert result.detail.loc[0, "Resultado"] == settings.RESULT_NO_OPTION
    assert result.output_df.loc[0, settings.COL_REASSIGNED] == ""


@case("Regla 2: solo se tocan los estados objetivo")
def test_status_filter():
    orders = make_orders(
        [
            order("P1", "S1", 1, status="SIN_STOCK"),
            order("P2", "S1", 1, status="SIN_DESPACHO"),
            order("P3", "S1", 1, status="ERROR_OPERADOR_LOGISTICO"),
        ]
    )
    stock = {("S1", "10"): 9}
    result = run(orders, stock, make_config())
    assert result.kpis.pedidos_a_reasignar == 2, result.kpis.pedidos_a_reasignar
    assert result.output_df.loc[2, "Reasig_Resultado"] == settings.RESULT_NOT_APPLICABLE
    assert result.output_df.loc[2, settings.COL_REASSIGNED] == ""


@case("Estados: 'sin stock' con espacio se normaliza igual que SIN_STOCK")
def test_status_normalization():
    orders = make_orders([order("P1", "S1", 1, status="sin stock")])
    stock = {("S1", "10"): 1}
    result = run(orders, stock, make_config())
    assert result.kpis.reasignados == 1, result.kpis.to_frame().to_dict()


@case("Stock de seguridad: reserva unidades intocables")
def test_safety_stock():
    orders = make_orders([order("P1", "S1", 1)])
    stock = {("S1", "10"): 2, ("S1", "20"): 5}
    result = run(orders, stock, make_config(stock_seguridad_global="2"))
    assert result.detail.loc[0, "Tienda reasignada"] == "TIENDA B", result.detail.loc[0].to_dict()


@case("Tope por tienda: max_unidades_por_tienda limita la carga")
def test_store_cap():
    orders = make_orders([order("P1", "S1", 1), order("P2", "S1", 1)])
    stock = {("S1", "10"): 9, ("S1", "20"): 9}
    result = run(orders, stock, make_config(max_unidades_por_tienda="1"))
    assigned = list(result.detail["Tienda reasignada"])
    assert assigned == ["TIENDA A", "TIENDA B"], assigned


@case("Reasignacion parcial: apagada por defecto, opcional por configuracion")
def test_partial():
    orders = make_orders([order("P1", "S1", 3)])
    stock = {("S1", "10"): 2}

    strict = run(orders, stock, make_config())
    assert strict.detail.loc[0, "Resultado"] == settings.RESULT_NO_OPTION

    loose = run(orders, stock, make_config(permitir_reasignacion_parcial="SI"))
    assert loose.detail.loc[0, "Resultado"] == engine.RESULT_PARTIAL
    assert loose.kpis.unidades_reasignadas == 2, loose.kpis.unidades_reasignadas


@case("ShGroup agrupado: las lineas de un despacho van a la misma tienda")
def test_group_by_shgroup():
    orders = make_orders(
        [
            order("P1", "S1", 1, shgroup="G1"),
            order("P1", "S2", 1, shgroup="G1"),
        ]
    )
    # TIENDA A solo tiene S1; solo TIENDA B cubre el despacho completo.
    stock = {("S1", "10"): 5, ("S1", "20"): 5, ("S2", "20"): 5}
    result = run(orders, stock, make_config(agrupar_por_shgroup="SI"))
    assigned = set(result.detail["Tienda reasignada"])
    assert assigned == {"TIENDA B"}, assigned


@case("ShGroup agrupado: sin cobertura total se puede exigir todo o nada")
def test_group_all_or_nothing():
    orders = make_orders(
        [
            order("P1", "S1", 1, shgroup="G1"),
            order("P1", "S2", 1, shgroup="G1"),
        ]
    )
    stock = {("S1", "10"): 5}  # S2 no existe en ninguna tienda

    split = run(orders, stock, make_config(agrupar_por_shgroup="SI"))
    assert split.detail.loc[0, "Tienda reasignada"] == "TIENDA A", split.detail.loc[0].to_dict()

    strict = run(
        orders,
        stock,
        make_config(agrupar_por_shgroup="SI", fallback_linea_si_grupo_falla="NO"),
    )
    assert set(strict.detail["Resultado"]) == {settings.RESULT_NO_OPTION}


@case("Regla 9: la columna de salida se crea si el archivo no la trae")
def test_output_column_created():
    orders = make_orders([order("P1", "S1", 1)])
    result = run(orders, {("S1", "10"): 1}, make_config())
    assert settings.COL_REASSIGNED in result.output_headers
    assert result.output_headers[: len(HEADERS)] == HEADERS
    assert result.output_df.loc[0, settings.COL_REASSIGNED] == "TIENDA A"


@case("Prioridad: una fila con el sitio exacto desplaza a la fila comodin")
def test_site_specificity():
    config = make_config()
    config.rules.append(
        {
            "sitio": "vansperu",
            "marca": "*",
            "cod_tienda": "30",
            "nom_tienda": "TIENDA C",
            "prioridad": 1,
            "activo": True,
            "stock_seguridad": 0,
            "max_unidades": 0,
        }
    )
    names = [rule.nom_tienda for rule in config.rules_for("vansperu", "Vans")]
    assert names == ["TIENDA C"], names
    generic = [rule.nom_tienda for rule in config.rules_for("columbiaperu", "Columbia")]
    assert generic == ["TIENDA A", "TIENDA B", "TIENDA C"], generic


@case("Stock: no se vacia una tienda si otra puede ceder y quedar con unidades")
def test_reserve_protects_last_unit():
    orders = make_orders([order("P1", "S1", 1)])
    # TIENDA A es la primera de la lista pero solo tiene 1 unidad.
    stock = {("S1", "10"): 1, ("S1", "20"): 5}
    result = run(orders, stock, make_config(reserva_por_tienda="1"))
    assert result.detail.loc[0, "Tienda reasignada"] == "TIENDA B", result.detail.loc[0].to_dict()
    assert result.detail.loc[0, "Stock restante"] == 4


@case("Stock: si TODAS tienen una sola unidad, recien ahi se toma la ultima")
def test_last_unit_as_last_resort():
    orders = make_orders([order("P1", "S1", 1)])
    stock = {("S1", "10"): 1, ("S1", "20"): 1}
    result = run(orders, stock, make_config(reserva_por_tienda="1"))
    # Se respeta la prioridad: gana TIENDA A, la primera de la lista.
    assert result.detail.loc[0, "Tienda reasignada"] == "TIENDA A", result.detail.loc[0].to_dict()
    assert result.detail.loc[0, "Stock restante"] == 0
    assert "Ultimo recurso" in result.detail.loc[0, "Detalle"], result.detail.loc[0, "Detalle"]


@case("Stock: reserva 2 exige que la tienda quede con 2 despues de ceder")
def test_reserve_two():
    orders = make_orders([order("P1", "S1", 1)])
    # A queda en 1 (insuficiente para reserva 2), B queda en 2.
    stock = {("S1", "10"): 2, ("S1", "20"): 3}
    result = run(orders, stock, make_config(reserva_por_tienda="2"))
    assert result.detail.loc[0, "Tienda reasignada"] == "TIENDA B", result.detail.loc[0].to_dict()


@case("Stock: reserva 0 desactiva la regla y manda la prioridad pura")
def test_reserve_off():
    orders = make_orders([order("P1", "S1", 1)])
    stock = {("S1", "10"): 1, ("S1", "20"): 9}
    result = run(orders, stock, make_config(reserva_por_tienda="0", ordenar_por_stock="NO"))
    assert result.detail.loc[0, "Tienda reasignada"] == "TIENDA A", result.detail.loc[0].to_dict()


@case("Empates: con la misma prioridad gana la tienda con mas stock")
def test_tiebreak_by_stock():
    config = make_config(ordenar_por_stock="SI", reserva_por_tienda="0")
    for rule in config.rules:  # las tres tiendas quedan en la misma banda
        rule["prioridad"] = 5
    orders = make_orders([order("P1", "S1", 1)])
    stock = {("S1", "10"): 2, ("S1", "20"): 9, ("S1", "30"): 4}
    result = run(orders, stock, config)
    assert result.detail.loc[0, "Tienda reasignada"] == "TIENDA B", result.detail.loc[0].to_dict()


@case("Empates: sin ordenar por stock gana la primera de la lista")
def test_tiebreak_without_stock():
    config = make_config(ordenar_por_stock="NO", reserva_por_tienda="0")
    for rule in config.rules:
        rule["prioridad"] = 5
    orders = make_orders([order("P1", "S1", 1)])
    stock = {("S1", "10"): 2, ("S1", "20"): 9}
    result = run(orders, stock, config)
    assert result.detail.loc[0, "Tienda reasignada"] == "TIENDA A", result.detail.loc[0].to_dict()


@case("Stock: la reserva se respeta pedido tras pedido en la misma corrida")
def test_reserve_across_orders():
    orders = make_orders([order("P1", "S1", 1), order("P2", "S1", 1)])
    # A tiene 2: cede una y queda en 1; para el segundo pedido ya no califica.
    stock = {("S1", "10"): 2, ("S1", "20"): 5}
    result = run(orders, stock, make_config(reserva_por_tienda="1", ordenar_por_stock="NO"))
    assert list(result.detail["Tienda reasignada"]) == ["TIENDA A", "TIENDA B"], list(
        result.detail["Tienda reasignada"]
    )


@case("Columnas: 'Sitio_1' se reconoce como 'Sitio' (sufijo numerico de Excel)")
def test_alias_numeric_suffix():
    headers = ["Order", "Estado", "SKU", "Unidades", "Sitio_1"]
    resolved = resolve_columns(headers)
    assert resolved.get(settings.COL_SITE) == "Sitio_1", resolved


@case("Columnas: un encabezado con mojibake se reconoce igual")
def test_alias_mojibake():
    # 'Método_de_Despacho' guardado como UTF-8 leido en latin-1.
    headers = ["Order", "Estado", "SKU", "Unidades", "MÃ©todo_de_Despacho"]
    resolved = resolve_columns(headers)
    assert resolved.get(settings.COL_SHIPPING_METHOD) == "MÃ©todo_de_Despacho", resolved


@case("Columnas: un encabezado con tilde real tambien se reconoce")
def test_alias_accent():
    headers = ["Order", "Estado", "SKU", "Unidades", "Método_de_Despacho"]
    resolved = resolve_columns(headers)
    assert resolved.get(settings.COL_SHIPPING_METHOD) == "Método_de_Despacho", resolved


@case("Estados: se pueden reasignar estados propios como PENDIENTE_ASIGNACION")
def test_custom_status():
    orders = make_orders([order("P1", "S1", 1, status="PENDIENTE_ASIGNACION")])
    stock = {("S1", "10"): 5}
    config = make_config(estados_objetivo="PENDIENTE_ASIGNACION")
    result = run(orders, stock, config)
    assert result.kpis.pedidos_a_reasignar == 1, result.kpis.pedidos_a_reasignar
    assert result.detail.loc[0, "Tienda reasignada"] == "TIENDA A"


@case("Errores: una fila sin SKU se marca como ERROR, no como sin stock")
def test_missing_sku():
    orders = make_orders([order("P1", "", 1)])
    result = run(orders, {}, make_config())
    assert result.detail.loc[0, "Resultado"] == settings.RESULT_ERROR
    assert result.kpis.errores == 1


@case("Sin opcion: se informa el mejor stock hallado, no un 0 enganoso")
def test_sin_opcion_informa_disponible():
    # Con 0 en las dos situaciones, desde la app se veian identicas: "no hay
    # stock en ningun lado" y "hay, pero repartido y no alcanza".
    config = make_config()
    orders = make_orders([order("P1", "A", 5)])
    orders[settings.COL_STATUS] = "SIN_STOCK"
    result = run(orders, {("A", "10"): 2, ("A", "20"): 3}, config)
    fila = result.detail.iloc[0]
    assert fila["Resultado"] == settings.RESULT_NO_OPTION
    assert int(fila["Stock disponible"]) == 3, fila.to_dict()
    assert "la mejor es TIENDA B con 3" in fila["Detalle"], fila["Detalle"]
    # La columna de trazabilidad del Excel lleva el mismo dato.
    assert int(result.output_df.iloc[0]["Reasig_Stock_Disponible"]) == 3


@case("Sin opcion: sin stock en ninguna tienda el 0 es real y se dice cuantas se miraron")
def test_sin_opcion_sin_stock_real():
    config = make_config()
    orders = make_orders([order("P1", "A", 1)])
    orders[settings.COL_STATUS] = "SIN_STOCK"
    fila = run(orders, {}, config).detail.iloc[0]
    assert int(fila["Stock disponible"]) == 0
    assert "Ninguna de las 3 tiendas" in fila["Detalle"], fila["Detalle"]


@case("2da opcion: el caso del correo, la 1a tienda tiene una sola unidad")
def test_segunda_opcion_una_unidad():
    # Si la tienda elegida rechaza el pedido, quien lo gestiona necesita a
    # donde mandarlo sin volver a correr la app.
    config = make_config()
    orders = make_orders([order("P1", "A", 1)])
    orders[settings.COL_STATUS] = "SIN_STOCK"
    fila = run(orders, {("A", "10"): 1, ("A", "20"): 4, ("A", "30"): 2}, config).detail.iloc[0]
    # Dentro de la misma banda gana la mas surtida, y la siguiente es el plan B.
    assert fila["Tienda reasignada"] == "TIENDA B" and fila["Stock disponible"] == 4
    assert fila["2da opcion"] == "TIENDA A", fila.to_dict()
    assert int(fila["Stock 2da opcion"]) == 1


@case("2da opcion: solo entra una tienda que cubra el pedido entero")
def test_segunda_opcion_debe_cubrir_todo():
    # Una tienda con la mitad no sirve de plan B: el pedido igual se cae.
    config = make_config()
    orders = make_orders([order("P1", "A", 3)])
    orders[settings.COL_STATUS] = "SIN_STOCK"
    fila = run(orders, {("A", "10"): 5, ("A", "20"): 1}, config).detail.iloc[0]
    assert fila["Tienda reasignada"] == "TIENDA A"
    assert fila["2da opcion"] == "", fila.to_dict()


@case("2da opcion: no consume stock ni se la reserva")
def test_segunda_opcion_no_consume():
    # Reservarle unidades dejaria stock sin usar en cada corrida.
    config = make_config()
    orders = make_orders([order("P1", "A", 1)])
    orders[settings.COL_STATUS] = "SIN_STOCK"
    result = run(orders, {("A", "10"): 5, ("A", "20"): 4}, config)
    usadas = result.store_summary.to_dict("records")
    assert usadas == [{"cod_tienda": "10", "nom_tienda": "TIENDA A", "unidades_reasignadas": 1}], usadas


@case("2da opcion: refleja lo ya comprometido por los pedidos anteriores")
def test_segunda_opcion_respeta_el_descuento():
    config = make_config()
    orders = make_orders([order("P1", "A", 1), order("P2", "A", 1), order("P3", "A", 1)])
    orders[settings.COL_STATUS] = "SIN_STOCK"
    detalle = run(orders, {("A", "10"): 2, ("A", "20"): 1}, config).detail
    # Las dos primeras van a A (2 unidades) con B de respaldo; la tercera se
    # lleva la unica unidad de B y ya no tiene respaldo que ofrecer.
    assert list(detalle["Tienda reasignada"]) == ["TIENDA A", "TIENDA A", "TIENDA B"]
    assert list(detalle["2da opcion"]) == ["TIENDA B", "TIENDA B", ""], detalle.to_dict("records")


@case("2da opcion: nunca es la tienda de origen del propio pedido")
def test_segunda_opcion_excluye_origen():
    config = make_config()
    orders = make_orders([order("P1", "A", 1, "TIENDA A", "10")])
    orders[settings.COL_STATUS] = "SIN_STOCK"
    fila = run(orders, {("A", "10"): 5, ("A", "20"): 4}, config).detail.iloc[0]
    assert fila["Tienda reasignada"] == "TIENDA B"
    assert fila["2da opcion"] == "", fila.to_dict()


@case("2da opcion: una fila SIN OPCION no ofrece respaldo")
def test_segunda_opcion_sin_opcion():
    config = make_config()
    orders = make_orders([order("P1", "A", 1)])
    orders[settings.COL_STATUS] = "SIN_STOCK"
    fila = run(orders, {}, config).detail.iloc[0]
    assert fila["Resultado"] == settings.RESULT_NO_OPTION
    assert fila["2da opcion"] == ""


@case("2da opcion: cuantas se informan se configura en Parametros")
def test_segunda_opcion_configurable():
    orders = make_orders([order("P1", "A", 1)])
    orders[settings.COL_STATUS] = "SIN_STOCK"
    stock = {("A", "10"): 5, ("A", "20"): 4, ("A", "30"): 2}

    dos = run(orders, stock, make_config(opciones_alternativas="2")).detail.iloc[0]
    assert dos["2da opcion"] == "TIENDA B" and dos["3ra opcion"] == "TIENDA C", dos.to_dict()

    cero = run(orders, stock, make_config(opciones_alternativas="0")).detail
    assert [c for c in cero.columns if "opcion" in c.lower()] == []


@case("2da opcion: viaja al Excel y se va con el resto de la trazabilidad")
def test_segunda_opcion_en_el_excel():
    config = make_config()
    orders = make_orders([order("P1", "A", 1)])
    orders[settings.COL_STATUS] = "SIN_STOCK"

    con = run(orders, {("A", "10"): 5, ("A", "20"): 4}, config)
    assert "Reasig_2da_Opcion" in con.output_headers
    assert con.output_df.iloc[0]["Reasig_2da_Opcion"] == "TIENDA B"
    assert con.output_df.iloc[0]["Reasig_Cod_2da_Opcion"] == "20"
    assert int(con.output_df.iloc[0]["Reasig_Stock_2da_Opcion"]) == 4

    # Sin trazabilidad el Excel de carga queda con las columnas originales.
    sin = engine.reassign(
        df=orders, headers=HEADERS, resolved=resolve_columns(HEADERS), config=config,
        stock_index={("A", "10"): 5, ("A", "20"): 4}, stock_cutoff="", include_trace=False,
    )
    assert [c for c in sin.output_headers if "Opcion" in c] == [], sin.output_headers


def main() -> int:
    passed, failed = 0, []
    for name, test in CASES:
        try:
            test()
        except AssertionError as exc:
            failed.append((name, str(exc)))
            print(f"  FALLO  {name}\n         {exc}")
        except Exception as exc:  # pragma: no cover
            failed.append((name, repr(exc)))
            print(f"  ERROR  {name}\n         {exc!r}")
        else:
            passed += 1
            print(f"  ok     {name}")

    print(f"\n{passed}/{len(CASES)} casos correctos.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
