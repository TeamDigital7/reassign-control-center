"""Pruebas de la app con el framework oficial de Streamlit (AppTest).

    python -m scripts.test_app_flow

Ejercita la aplicacion real, sin navegador: pantalla de acceso, credenciales
correctas e incorrectas, y el arranque del flujo con la sesion iniciada.
"""

from __future__ import annotations

import sys

from streamlit.testing.v1 import AppTest

from config import settings

APP = str(settings.BASE_DIR / "app.py")
SECRETS = {"app_auth": {"users": {"hugo.camara@forus.pe": "clave-de-prueba"}}}

CASES: list[tuple[str, object]] = []


def case(name: str):
    def decorator(fn):
        CASES.append((name, fn))
        return fn

    return decorator


def start(secrets: dict | None = None) -> AppTest:
    app = AppTest.from_file(APP, default_timeout=60)
    if secrets:
        for section, values in secrets.items():
            app.secrets[section] = values
    return app.run()


@case("Sin [app_auth] la app arranca abierta, sin pantalla de acceso")
def test_open_app():
    # AppTest hereda el secrets.toml real del proyecto, asi que la seccion se
    # vacia a proposito para reproducir un despliegue sin login configurado.
    app = start({"app_auth": {}})
    body = " ".join(element.value for element in app.markdown)
    assert "Sistema exclusivo" not in body, "no deberia pedir login"
    assert "authenticated" not in app.session_state
    assert not app.exception, app.exception


@case("Con [app_auth] se muestra la pantalla de acceso")
def test_login_shown():
    app = start(SECRETS)
    body = " ".join(element.value for element in app.markdown)
    assert "Reassign Control Center" in body, "falta el nombre de la app"
    assert "Sistema exclusivo para personal autorizado" in body
    assert len(app.text_input) == 2, f"se esperaban 2 campos, hay {len(app.text_input)}"
    assert not app.exception, app.exception


@case("Credenciales correctas: entra y queda la sesion iniciada")
def test_login_ok():
    app = start(SECRETS)
    app.text_input[0].set_value("hugo.camara@forus.pe")
    app.text_input[1].set_value("clave-de-prueba")
    app.button[0].click().run()
    assert app.session_state["authenticated"] is True, "no autentico"
    assert app.session_state["auth_user"] == "hugo.camara@forus.pe"
    assert not app.exception, app.exception


@case("El correo se normaliza: mayusculas y espacios no bloquean el acceso")
def test_login_normalizes_email():
    app = start(SECRETS)
    app.text_input[0].set_value("  Hugo.Camara@Forus.pe  ")
    app.text_input[1].set_value("clave-de-prueba")
    app.button[0].click().run()
    assert app.session_state["authenticated"] is True


@case("Credenciales incorrectas: no entra y avisa")
def test_login_fail():
    app = start(SECRETS)
    app.text_input[0].set_value("hugo.camara@forus.pe")
    app.text_input[1].set_value("clave-equivocada")
    app.button[0].click().run()
    assert "authenticated" not in app.session_state, "no debio autenticar"
    assert app.error, "faltaba el mensaje de error"
    assert "incorrect" in app.error[0].value.lower()


@case("Usuario inexistente: no entra")
def test_unknown_user():
    app = start(SECRETS)
    app.text_input[0].set_value("otra.persona@forus.pe")
    app.text_input[1].set_value("clave-de-prueba")
    app.button[0].click().run()
    assert "authenticated" not in app.session_state
    assert app.error


@case("Con sesion iniciada aparece el flujo, los pasos y el cargador de archivo")
def test_flow_after_login():
    app = start(SECRETS)
    app.session_state["authenticated"] = True
    app.session_state["auth_user"] = "hugo.camara@forus.pe"
    app.run()
    body = " ".join(element.value for element in app.markdown)
    assert "Reasignacion de pedidos sin stock" in body, "falta la cabecera principal"
    assert "Subir archivo de pedidos" in body, "falta el paso 1"
    assert "Subir archivo" in body and "Descargar" in body, "falta el rail de pasos"
    assert app.file_uploader, "falta el cargador del archivo de pedidos"
    assert not app.exception, app.exception


@case("La prioridad versionada se carga sola y se ve en la barra lateral")
def test_priority_visible():
    from core.priority import load_priority

    app = start(SECRETS)
    app.session_state["authenticated"] = True
    app.run()
    sidebar_text = " ".join(element.value for element in app.sidebar.markdown)
    assert "SIN_STOCK, SIN_DESPACHO" in sidebar_text, "no se ven los estados objetivo"

    # La barra reporta el numero real de tiendas del archivo del repositorio.
    tiendas = load_priority().store_count
    assert tiendas > 0, "la prioridad versionada no se cargo"
    assert f"{tiendas} tiendas" in sidebar_text, f"no aparece '{tiendas} tiendas'"
    assert not app.exception, app.exception


@case("Sin avisos de configuracion cuando la prioridad ya esta cargada")
def test_no_config_noise():
    app = start(SECRETS)
    app.session_state["authenticated"] = True
    app.run()
    cuerpo = " ".join(element.value for element in app.markdown)
    for ruido in ("No se encontro", "sube tu propia version", "prioridad_tiendas.ejemplo"):
        assert ruido not in cuerpo, f"la app sigue pidiendo configuracion: '{ruido}'"


@case("Cerrar sesion devuelve a la pantalla de acceso")
def test_logout():
    app = start(SECRETS)
    app.session_state["authenticated"] = True
    app.session_state["auth_user"] = "hugo.camara@forus.pe"
    app.run()
    logout = [button for button in app.sidebar.button if "Cerrar" in button.label]
    assert logout, "no hay boton de cerrar sesion"
    logout[0].click().run()
    assert "authenticated" not in app.session_state, "la sesion sigue abierta"


# ---------------------------------------------------------------------------
# Pasos 3 a 5: consulta de stock y reasignacion
# ---------------------------------------------------------------------------
def pedidos() -> "excel_io.WorkbookPayload":
    """Archivo de pedidos minimo, con el SKU en las tres formas que llegan."""
    import pandas as pd

    from core import excel_io

    headers = ["Order", "Sitio", "Estado", "Marca", "SKU", "Unidades"]
    filas = [
        ["vns-1", "columbiaperu", "SIN_STOCK", "Columbia", "0005438957", 1],
        ["vns-2", "columbiaperu", "SIN_STOCK", "Columbia", 5438958.0, 1],
        ["vns-3", "columbiaperu", "SIN_STOCK", "Columbia", 5438959, 1],
    ]
    return excel_io.WorkbookPayload(
        df=pd.DataFrame(filas, columns=headers, dtype=object),
        headers=headers,
        sheet_name="Hoja1",
        source_name="pedidos.xlsx",
        source_format="xlsx",
    )


def con_pedidos(stock=None, stock_skus=None) -> AppTest:
    """App con la sesion ya cargada hasta el paso que se quiera probar."""
    from core.priority import load_priority
    from core.validation import validate

    payload = pedidos()
    config = load_priority()
    app = AppTest.from_file(APP, default_timeout=60)
    app.secrets["app_auth"] = {}
    app.session_state["payload"] = payload
    app.session_state["report"] = validate(payload.df, payload.headers, config)
    app.session_state["estados_objetivo_sel"] = ["SIN_STOCK"]
    if stock is not None:
        app.session_state["stock"] = stock
        app.session_state["stock_skus"] = stock_skus
    return app.run()


def stock_de_prueba():
    """Un SKU con unidades, uno en cero y uno que la fuente no conoce."""
    from core.stock_source import ManualStockSource
    import pandas as pd

    frame = pd.DataFrame(
        [
            {"sku": "5438957", "cod_tienda": "59", "stock": 4, "fecha_corte": "2026-08-20"},
            {"sku": "5438958", "cod_tienda": "59", "stock": 0, "fecha_corte": "2026-08-20"},
        ]
    )
    return ManualStockSource(frame, True).fetch(["5438957", "5438958", "5438959"])


@case("Paso 3: con el archivo validado aparece el boton de consultar stock")
def test_paso_3():
    app = con_pedidos()
    etiquetas = [button.label for button in app.button]
    assert "Consultar stock" in etiquetas, etiquetas
    assert not app.exception, app.exception


@case("Paso 4: 'SKU con stock' cuenta solo los que traen unidades")
def test_paso_4_kpi():
    app = con_pedidos(stock_de_prueba(), ["5438957", "5438958", "5438959"])
    cuerpo = " ".join(element.value for element in app.markdown)
    # 3 consultados: uno con unidades, uno en cero, uno sin respuesta.
    assert "1 sin respuesta" in cuerpo, cuerpo[-1500:]
    assert "1 en cero" in cuerpo, cuerpo[-1500:]
    assert "Ejecutar reasignacion" in [button.label for button in app.button]
    assert not app.exception, app.exception


@case("Paso 4: el detalle por SKU separa 'sin respuesta' de 'en cero'")
def test_paso_4_detalle():
    app = con_pedidos(stock_de_prueba(), ["5438957", "5438958", "5438959"])
    situaciones = set()
    for frame in app.dataframe:
        if "situacion" in getattr(frame.value, "columns", []):
            situaciones |= set(frame.value["situacion"])
    assert situaciones == {"CON STOCK", "EN CERO", "SIN RESPUESTA"}, situaciones
    assert not app.exception, app.exception


@case("Stock viejo: si cambian los SKU a consultar, la foto anterior se descarta")
def test_stock_desactualizado():
    # El stock guardado corresponde a otra seleccion de estados: si se
    # conservara, los SKU nuevos apareceria como "sin stock" sin haberse
    # consultado jamas.
    app = con_pedidos(stock_de_prueba(), ["999999"])
    assert "stock" not in app.session_state, "se reutilizo una consulta que ya no aplica"
    cuerpo = " ".join(element.value for element in app.markdown)
    assert "Cambio la lista de SKU a consultar" in cuerpo, cuerpo[-1200:]
    assert not app.exception, app.exception


@case("Paso 5: la reasignacion corre desde la app y deja el archivo listo")
def test_paso_5():
    app = con_pedidos(stock_de_prueba(), ["5438957", "5438958", "5438959"])
    for button in app.button:
        if button.label == "Ejecutar reasignacion":
            button.click().run()
            break
    else:  # pragma: no cover
        raise AssertionError("no se encontro el boton de reasignacion")
    assert not app.exception, app.exception
    result = app.session_state["result"]
    # Solo el SKU con unidades se puede reasignar; los otros dos quedan marcados.
    assert result.kpis.reasignados == 1, result.kpis
    assert result.kpis.sin_stock == 2, result.kpis
    etiquetas = [button.label for button in app.download_button]
    assert "Descargar Excel de carga" in etiquetas, etiquetas


@case("Version mezclada: no se cae con un TypeError de argumentos")
def test_call_compat_modulo_viejo():
    # Streamlit re-ejecuta app.py en cada interaccion pero conserva en memoria
    # los modulos ya importados. Tras un despliegue archivo por archivo puede
    # quedar un app.py nuevo llamando a un core/ viejo, y la corrida se caia
    # con "secrets_to_source() takes from 2 to 3 positional arguments but 4
    # were given", que no le dice nada a quien usa la app.
    import app

    def vieja(secrets, include_central_warehouse, central_codes=None):
        return ("vieja", central_codes)

    assert app.call_compat(vieja, {}, True, "320", "solo_bodega") == ("vieja", "320")

    def nueva(secrets, include_central_warehouse, central_codes=None, formula=None):
        return ("nueva", central_codes, formula)

    assert app.call_compat(nueva, {}, True, "320", "solo_bodega") == (
        "nueva", "320", "solo_bodega",
    )


@case("Version mezclada: se detecta y se avisa, no pasa en silencio")
def test_stale_modules():
    import app
    from core.stock_source import secrets_to_source

    # Con el codigo al dia no hay nada que avisar.
    assert app.stale_modules() is False
    assert app._positional_limit(secrets_to_source) >= 4

    def vieja(secrets, include_central_warehouse, central_codes=None):
        return None

    assert app._positional_limit(vieja) == 3


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
