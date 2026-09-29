"""Pruebas del cruce de stock: que el SKU del pedido encuentre su stock.

    python -m scripts.test_stock_match

Cubren los tres motivos por los que la app puede decir "sin stock" teniendo
stock:

1. **El codigo no cruza.** El Excel trae `0005438957` y BigQuery guarda
   `5438957` (o al reves, `5438957.0` si el campo es numerico). Si cada lado
   compara su forma cruda, no hay coincidencia y el resultado es cero filas.
2. **Las unidades se pierden al consolidar.** Si la fuente devuelve el mismo
   par (SKU, tienda) en varias filas, quedarse con la ultima en vez de sumarlas
   descarta unidades reales.
3. **El stock queda viejo.** El stock consultado vale solo para los SKU que se
   pidieron; con otra seleccion de estados, la foto en memoria queda corta.
"""

from __future__ import annotations

import sys

import pandas as pd

from config import settings
from core.excel_io import normalize_sku
from core.stock_source import (
    ManualStockSource,
    STOCK_QUERY,
    build_stock_index,
    build_stock_query,
    central_warehouse_codes,
    classify_reserve_columns,
    consolidate,
    stock_coverage,
    stock_cutoff,
)

CASES: list[tuple[str, object]] = []


def case(name: str):
    def decorator(fn):
        CASES.append((name, fn))
        return fn

    return decorator


def archivo(filas: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(filas)


# --- 1. El codigo cruza sin importar la forma en que venga ------------------
@case("SKU: el archivo trae ceros a la izquierda y la fuente no")
def test_ceros_a_la_izquierda():
    stock = ManualStockSource(
        archivo([{"sku": "5438957", "cod_tienda": "59", "stock": 4}]), True
    ).fetch(["0005438957"])
    assert len(stock) == 1, stock.to_dict("records")
    assert build_stock_index(stock) == {("5438957", "59"): 4}


@case("SKU: la fuente trae ceros a la izquierda y el archivo no")
def test_ceros_del_lado_de_la_fuente():
    stock = ManualStockSource(
        archivo([{"sku": "0005438957", "cod_tienda": "59", "stock": 4}]), True
    ).fetch(["5438957"])
    assert len(stock) == 1, stock.to_dict("records")
    assert stock.loc[0, "sku"] == "5438957"


@case("SKU: el `.0` de un campo numerico no rompe el cruce")
def test_decimal_cero():
    stock = ManualStockSource(
        archivo([{"sku": "5438957.0", "cod_tienda": "59", "stock": 4}]), True
    ).fetch([5438957.0])
    assert len(stock) == 1, stock.to_dict("records")
    assert stock.loc[0, "sku"] == "5438957"


@case("SKU: un codigo alfanumerico conserva su cero inicial")
def test_alfanumerico_conserva_ceros():
    # `0A12` no es un numero: ahi el cero puede ser parte del codigo y no se
    # toca. Si se recortara, dejaria de cruzar con la fuente.
    assert normalize_sku("0A12") == "0A12"
    stock = ManualStockSource(
        archivo([{"sku": "0A12", "cod_tienda": "59", "stock": 2}]), True
    ).fetch(["0A12"])
    assert len(stock) == 1, stock.to_dict("records")


@case("SKU: normalize_sku es idempotente")
def test_idempotente():
    for value in ["0005438957", "5438957.0", " 5438957 ", 5438957.0, "0A12", "000"]:
        una = normalize_sku(value)
        assert normalize_sku(una) == una, (value, una)


@case("BigQuery: la consulta normaliza el SKU del mismo modo que la app")
def test_consulta_normaliza_en_sql():
    query = build_stock_query(settings.DEFAULT_STOCK_TABLE)
    assert settings.DEFAULT_STOCK_TABLE in query
    assert "{" not in query, "quedaron marcadores sin reemplazar en la consulta"
    # El filtro compara la forma canonica, no `CAST(id_producto AS STRING)` a secas.
    assert "WHERE sku IN UNNEST(@skus)" in query
    for pieza in ("UPPER(TRIM(CAST(s.id_producto AS STRING)))", "[.]0+$", "^0*([0-9]+?)$"):
        assert pieza in query, pieza


@case("BigQuery: la consulta sigue siendo de solo lectura")
def test_consulta_solo_lectura():
    prohibido = ("INSERT", "UPDATE", "DELETE", "MERGE", "CREATE", "DROP", "TRUNCATE")
    texto = STOCK_QUERY.upper()
    for palabra in prohibido:
        assert palabra not in texto, palabra


# --- 2. Las unidades no se pierden ------------------------------------------
@case("Repetidos: el mismo par (SKU, tienda) suma en vez de pisarse")
def test_repetidos_suman():
    stock = ManualStockSource(
        archivo(
            [
                {"sku": "5438957", "cod_tienda": "59", "stock": 3, "fecha_corte": "2026-08-20"},
                {"sku": "5438957", "cod_tienda": "59", "stock": 4, "fecha_corte": "2026-08-20"},
            ]
        ),
        True,
    ).fetch(["5438957"])
    assert len(stock) == 1, stock.to_dict("records")
    assert build_stock_index(stock) == {("5438957", "59"): 7}


@case("Repetidos: una consulta propia con filas abiertas tambien se consolida")
def test_consolidate_directo():
    crudo = pd.DataFrame(
        [
            {"sku": "A", "cod_tienda": "59", "stock_tiendas": 2, "stock_bodega": 0,
             "stock": 2, "fecha_corte": "2026-08-20"},
            {"sku": "A", "cod_tienda": "59", "stock_tiendas": 5, "stock_bodega": 0,
             "stock": 5, "fecha_corte": "2026-08-20"},
            {"sku": "A", "cod_tienda": "88", "stock_tiendas": 1, "stock_bodega": 0,
             "stock": 1, "fecha_corte": "2026-08-20"},
        ]
    )
    out = consolidate(crudo)
    assert len(out) == 2, out.to_dict("records")
    assert build_stock_index(out) == {("A", "59"): 7, ("A", "88"): 1}


@case("Bodega central: el disponible es su stock_bodega; la tienda usa el suyo")
def test_bodega_central():
    datos = archivo(
        [
            {"sku": "A", "cod_tienda": "320", "stock_tiendas": 1, "stock_bodega": 9},
            {"sku": "A", "cod_tienda": "59", "stock_tiendas": 1, "stock_bodega": 9},
        ]
    )
    stock = ManualStockSource(datos, True).fetch(["A"])
    index = build_stock_index(stock)
    # En una bodega central manda `stock_bodega` (9), no la suma con el piso.
    # En una tienda fisica manda `stock_tiendas` (1) y `stock_bodega` es otro
    # almacen que no se despacha desde ahi.
    assert index == {("A", "320"): 9, ("A", "59"): 1}, index


@case("Bodega central: se puede declarar mas de una sin tocar codigo")
def test_bodegas_centrales_configurables():
    assert central_warehouse_codes() == {"320"}
    assert central_warehouse_codes("320, 400") == {"320", "400"}
    datos = archivo([{"sku": "A", "cod_tienda": "400", "stock_tiendas": 1, "stock_bodega": 9}])
    # Sin declararla, la 400 es una tienda fisica: solo cuenta su piso.
    assert build_stock_index(ManualStockSource(datos, True).fetch(["A"])) == {("A", "400"): 1}
    # Declarada como central, cuenta su bodega.
    con_400 = ManualStockSource(datos, True, ("320", "400")).fetch(["A"])
    assert build_stock_index(con_400) == {("A", "400"): 9}


@case("Negativos: se suman antes de aplicar el piso en cero")
def test_negativos():
    # Una tienda con +5 en una fila y -3 en otra tiene 2 unidades, no 5.
    datos = archivo(
        [
            {"sku": "A", "cod_tienda": "59", "stock_tiendas": 5},
            {"sku": "A", "cod_tienda": "59", "stock_tiendas": -3},
            {"sku": "B", "cod_tienda": "59", "stock_tiendas": -4},
        ]
    )
    stock = ManualStockSource(datos, True).fetch(["A", "B"])
    assert build_stock_index(stock) == {("A", "59"): 2}, stock.to_dict("records")


# --- 3. Que se ve cuando un SKU no trae stock -------------------------------
@case("Cobertura: se distingue 'no vino en la consulta' de 'vino en cero'")
def test_cobertura():
    stock = ManualStockSource(
        archivo(
            [
                {"sku": "A", "cod_tienda": "59", "stock": 4},
                {"sku": "B", "cod_tienda": "59", "stock": 0},
            ]
        ),
        True,
    ).fetch(["A", "B", "C"])
    cobertura = stock_coverage(stock, ["A", "B", "C"]).set_index("sku")
    assert cobertura.loc["A", "situacion"] == "CON STOCK"
    assert int(cobertura.loc["A", "unidades"]) == 4
    assert cobertura.loc["B", "situacion"] == "EN CERO"
    assert cobertura.loc["C", "situacion"] == "SIN RESPUESTA"


@case("Cobertura: compara el SKU ya normalizado, no el crudo del archivo")
def test_cobertura_normaliza():
    stock = ManualStockSource(
        archivo([{"sku": "5438957", "cod_tienda": "59", "stock": 4}]), True
    ).fetch(["0005438957"])
    cobertura = stock_coverage(stock, ["0005438957"])
    assert list(cobertura["sku"]) == ["5438957"]
    assert cobertura.loc[0, "situacion"] == "CON STOCK"


@case("Cobertura: sin ninguna fila, todos los SKU quedan como SIN RESPUESTA")
def test_cobertura_vacia():
    cobertura = stock_coverage(pd.DataFrame(), ["A", "B"])
    assert list(cobertura["situacion"]) == ["SIN RESPUESTA", "SIN RESPUESTA"]


@case("Fecha de corte: se informa como fecha, no como texto")
def test_cutoff_dd_mm_yyyy():
    # Como texto, "31/12/2025" es mayor que "20/08/2026". Como fecha, no.
    stock = ManualStockSource(
        archivo(
            [
                {"sku": "A", "cod_tienda": "59", "stock": 1, "fecha_corte": "31/12/2025"},
                {"sku": "A", "cod_tienda": "88", "stock": 1, "fecha_corte": "20/08/2026"},
            ]
        ),
        True,
    ).fetch(["A"])
    assert stock_cutoff(stock) == "20/08/2026", stock.to_dict("records")


@case("Formula de bodega central: sumar, solo_bodega y restar_tiendas")
def test_formula_bodega_central():
    # Cual corresponde depende de como modela el origen los dos almacenes;
    # por eso se elige en la hoja Parametros y no esta fija en el codigo.
    datos = pd.DataFrame(
        [
            {"sku": "A", "cod_tienda": "320", "stock_tiendas": 30, "stock_bodega": 100,
             "fecha_corte": "2026-09-03"},
        ]
    )
    def disponible(modo):
        stock = ManualStockSource(datos, True, ("320",), modo).fetch(["A"])
        return int(stock.loc[0, "stock"])

    assert disponible("solo_bodega") == 100
    assert disponible("sumar") == 130
    assert disponible("restar_tiendas") == 70
    # Un valor desconocido no rompe: cae en el default.
    assert disponible("cualquier_cosa") == 100


@case("La formula solo aplica a la bodega central, nunca a una tienda fisica")
def test_formula_no_toca_tienda_fisica():
    datos = pd.DataFrame(
        [
            {"sku": "A", "cod_tienda": "59", "stock_tiendas": 4, "stock_bodega": 77,
             "fecha_corte": "2026-09-03"},
        ]
    )
    for modo in ("sumar", "solo_bodega", "restar_tiendas"):
        stock = ManualStockSource(datos, True, ("320",), modo).fetch(["A"])
        assert int(stock.loc[0, "stock"]) == 4, (modo, stock.to_dict("records"))


@case("Reservas: cada almacen descuenta la columna de reserva que le toca")
def test_reservas_por_almacen():
    datos = archivo(
        [
            {"sku": "A", "cod_tienda": "320", "stock_tiendas": 30, "stock_bodega": 100,
             "reserva_tiendas": 5, "reserva_bodega": 40},
            {"sku": "A", "cod_tienda": "59", "stock_tiendas": 10, "stock_bodega": 77,
             "reserva_tiendas": 3, "reserva_bodega": 50},
        ]
    )
    index = build_stock_index(ManualStockSource(datos, True).fetch(["A"]))
    # 320 es central: 100 de bodega menos 40 reservados.
    # 59 es tienda: 10 de piso menos 3 reservados; su bodega no se toca.
    assert index == {("A", "320"): 60, ("A", "59"): 7}, index


@case("Reservas: una reserva sin apellido aplica al almacen que use la fila")
def test_reserva_generica():
    datos = archivo(
        [
            {"sku": "B", "cod_tienda": "320", "stock_tiendas": 0, "stock_bodega": 50,
             "reserva": 20},
            {"sku": "B", "cod_tienda": "59", "stock_tiendas": 8, "stock_bodega": 0,
             "reserva": 3},
        ]
    )
    index = build_stock_index(ManualStockSource(datos, True).fetch(["B"]))
    assert index == {("B", "320"): 30, ("B", "59"): 5}, index


@case("Reservas: se reconocen por el encabezado, no por un nombre fijo")
def test_reservas_por_encabezado():
    de_tiendas, de_bodega = classify_reserve_columns(
        ["id_producto", "stock_tiendas", "stock_bodega", "reserva_tiendas",
         "reserva_bodega", "reserva", "Stock_Reservado_Tienda", "fecha_corte"]
    )
    assert de_tiendas == ["reserva_tiendas", "reserva", "Stock_Reservado_Tienda"], de_tiendas
    assert de_bodega == ["reserva_bodega", "reserva"], de_bodega
    # Una tabla sin columnas de reserva no resta nada.
    assert classify_reserve_columns(["sku", "stock_bodega"]) == ([], [])


@case("Reservas: una reserva mayor que el stock no deja el disponible negativo")
def test_reserva_mayor_que_stock():
    datos = archivo([{"sku": "C", "cod_tienda": "59", "stock_tiendas": 2, "reserva_tiendas": 9}])
    stock = ManualStockSource(datos, True).fetch(["C"])
    assert int(stock.loc[0, "stock"]) == 0, stock.to_dict("records")
    assert build_stock_index(stock) == {}


@case("Reservas: la consulta de BigQuery las descuenta dentro del SQL")
def test_reservas_en_el_sql():
    columnas = ["id_producto", "codigo_tienda", "stock_tiendas", "stock_bodega",
                "reserva_tiendas", "reserva_bodega", "fecha_corte"]
    consulta = build_stock_query("p.d.t", columnas)
    assert "s.reserva_tiendas" in consulta, consulta
    assert "s.reserva_bodega" in consulta, consulta
    # Sin conocer las columnas, la consulta no inventa restas.
    simple = build_stock_query("p.d.t")
    assert "reserva" not in simple, simple


def main() -> int:
    passed, failed = 0, []
    for name, test in CASES:
        try:
            test()
        except AssertionError as exc:
            failed.append(name)
            print(f"  FALLO  {name}\n         {exc}")
        except Exception as exc:  # pragma: no cover
            failed.append(name)
            print(f"  ERROR  {name}\n         {exc!r}")
        else:
            passed += 1
            print(f"  ok     {name}")

    print(f"\n{passed}/{len(CASES)} casos correctos.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
