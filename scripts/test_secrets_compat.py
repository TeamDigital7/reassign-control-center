"""Compatibilidad con los secrets de Catalogo Control Center.

    python -m scripts.test_secrets_compat

El equipo reutiliza el mismo `secrets.toml` entre aplicaciones internas. Estas
pruebas fijan ese contrato para que copiar los bloques `[bigquery]`,
`[gcp_service_account]` y `[app_auth]` no requiera editar nada.

El punto mas delicado: en Catalogo Control Center la clave `table` apunta a la
tabla ARTI (maestro de productos), no al stock. Si esta app la tomara como
tabla de stock, consultaria la tabla equivocada.
"""

from __future__ import annotations

import sys

import pandas as pd

from config import settings
from core.stock_source import (
    ManualStockSource,
    _finalize,
    build_stock_query,
    is_bigquery_configured,
    resolve_stock_table,
    secrets_to_source,
    sku_variants,
)

# Bloque [bigquery] tal como vive en Catalogo Control Center.
CCC_BIGQUERY = {
    "enabled": True,
    "project_id": "forus-analitica-prod",
    "job_project_id": "forus-analitica-prod",
    "table": "forus-analitica-prod-datalake.bronze.stg_pe_central_arti",
    "product_master_table": "forus-analitica-prod-datalake.bronze.stg_pe_central_arti",
    "service_account_info": {"project_id": "forus-analitica-prod", "client_email": "x@y.iam"},
}

CASES: list[tuple[str, object]] = []


def case(name: str):
    def decorator(fn):
        CASES.append((name, fn))
        return fn

    return decorator


@case("La clave 'table' de Catalogo (tabla ARTI) NO se usa como tabla de stock")
def test_arti_table_ignored():
    table = resolve_stock_table(CCC_BIGQUERY)
    assert "arti" not in table.lower(), f"se resolvio la tabla ARTI como stock: {table}"
    assert table == settings.DEFAULT_STOCK_TABLE, table


@case("'stock_table' explicito manda sobre el valor por defecto")
def test_explicit_stock_table():
    secrets = {**CCC_BIGQUERY, "stock_table": "proyecto.dataset.mi_stock"}
    assert resolve_stock_table(secrets) == "proyecto.dataset.mi_stock"


@case("Sin 'stock_table' se usa stg_pe_central_stock_bi")
def test_default_table():
    assert resolve_stock_table({}) == settings.DEFAULT_STOCK_TABLE
    assert "stg_pe_central_stock_bi" in settings.DEFAULT_STOCK_TABLE


@case("Los secrets de Catalogo se consideran BigQuery configurado")
def test_configured():
    assert is_bigquery_configured(CCC_BIGQUERY) is True


@case("enabled=false apaga BigQuery aunque haya credenciales")
def test_disabled():
    assert is_bigquery_configured({**CCC_BIGQUERY, "enabled": False}) is False
    assert is_bigquery_configured({**CCC_BIGQUERY, "enabled": "no"}) is False


@case("Sin credenciales no se considera configurado")
def test_no_credentials():
    assert is_bigquery_configured({"enabled": True}) is False
    assert is_bigquery_configured({}) is False


@case("El proveedor se arma con project_id y job_project_id de Catalogo")
def test_source_built():
    source = secrets_to_source(CCC_BIGQUERY, include_central_warehouse=True)
    assert source.project_id == "forus-analitica-prod"
    assert source.job_project_id == "forus-analitica-prod"
    assert source.table == settings.DEFAULT_STOCK_TABLE
    assert source.service_account_info["client_email"] == "x@y.iam"


@case("Se acepta la service account como JSON en texto")
def test_service_account_json():
    secrets = {"project_id": "p", "service_account_json": '{"client_email": "z@y.iam"}'}
    source = secrets_to_source(secrets, include_central_warehouse=True)
    assert source.service_account_info == {"client_email": "z@y.iam"}


@case("Una consulta con el esquema de stg_pe_central_stock_bi se entiende igual")
def test_query_schema_aliases():
    # Nombres tal como los devuelve el stock_query de Catalogo Control Center.
    raw = pd.DataFrame(
        [
            {"id_producto": 5438957, "codigo_tienda": 151, "stock_tiendas": 3,
             "stock_bodega": 0, "fecha_corte": "2026-08-17", "CONCAT_TIENDA": "3-151"},
            {"id_producto": 5438957, "codigo_tienda": 320, "stock_tiendas": 1,
             "stock_bodega": 4, "fecha_corte": "2026-08-17", "CONCAT_TIENDA": "1-320"},
        ]
    )
    out = _finalize(raw, include_central_warehouse=True)
    assert list(out.columns[:2]) == ["sku", "cod_tienda"], list(out.columns)
    assert out.loc[0, "sku"] == "5438957"
    assert out.loc[0, "cod_tienda"] == "151"
    # En tienda solo cuenta el stock de sala...
    assert out.loc[0, "stock"] == 3
    # ...y en la bodega central 320 cuenta su propio stock_bodega (4), no la
    # suma con el piso: son la misma mercaderia contada dos veces.
    assert out.loc[1, "stock"] == 4


@case("Una consulta sin las columnas minimas falla con un mensaje claro")
def test_bad_schema():
    raw = pd.DataFrame([{"producto": 1, "unidades": 2}])
    try:
        _finalize(raw, include_central_warehouse=True)
    except ValueError as exc:
        assert "sku" in str(exc) and "cod_tienda" in str(exc), str(exc)
    else:
        raise AssertionError("deberia haber fallado")


@case("El archivo de stock manual sigue funcionando con nombres propios")
def test_manual_source():
    raw = pd.DataFrame([{"sku": "5438957", "cod_tienda": "59", "stock": 2}])
    out = ManualStockSource(raw, include_central_warehouse=True).fetch(["5438957"])
    assert len(out) == 1 and out.loc[0, "stock"] == 2


@case("Una consulta propia recibe tambien las variantes crudas del SKU")
def test_variantes_para_consulta_propia():
    # Una consulta propia se ejecuta tal cual. Si compara el valor crudo
    # (`5438957.0` de un FLOAT, `0005438957` de un texto) contra el SKU ya
    # normalizado, no devuelve nada. Mandar las variantes arregla ese cruce
    # sin reescribir la consulta del usuario.
    variantes = sku_variants("5438957")
    assert "5438957" in variantes
    assert "5438957.0" in variantes
    assert "0005438957" in variantes
    # Un codigo alfanumerico no se toca: inventar variantes no tendria sentido.
    assert sku_variants("ABC-12") == ["ABC-12"]
    assert sku_variants("") == []


@case("La consulta propia del repo solo necesita el SKU canonico")
def test_sin_variantes_en_la_consulta_del_repo():
    # La consulta del repo canoniza los dos lados dentro de BigQuery, asi que
    # mandar variantes seria ruido.
    consulta = build_stock_query("p.d.t")
    assert "sku IN UNNEST(@skus)" in consulta
    assert "REGEXP_REPLACE(UPPER(TRIM(CAST(s.id_producto AS STRING)))" in consulta


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
