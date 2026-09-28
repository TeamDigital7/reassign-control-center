# Reassign Control Center

Aplicacion Streamlit para automatizar la **reasignacion de pedidos que no pudieron
despacharse** (`SIN_STOCK` / `SIN_DESPACHO`) hacia la tienda de mayor prioridad que
tenga stock disponible.

Parte de la familia de herramientas internas de Forus Peru, con el mismo lenguaje
visual que Catalogo Control Center y Revenue Control Center.

```
Subir archivo -> Validar -> Consultar BigQuery -> Reasignar -> Revisar -> Descargar
```

> **BigQuery es solo lectura.** La app consulta stock; nunca lo modifica. El
> descuento de unidades ocurre en memoria durante la corrida.

---

## Que hace

1. Lee el Excel de pedidos (`.xls` o `.xlsx`) conservando **todas** sus columnas.
2. Identifica los pedidos a reasignar. **Los estados se eligen en la propia
   interfaz**, con los que trae el archivo: por defecto `SIN_STOCK` y
   `SIN_DESPACHO`, pero sirve cualquiera (`PENDIENTE_ASIGNACION`, etc.).
   Tolera `sin stock`, `Sin-Stock`, `SIN_STOCK` como el mismo estado.
3. Toma el SKU de cada pedido y consulta el stock por tienda en BigQuery.
   **El SKU se canoniza en los dos lados** (archivo y consulta): `0005438957`,
   `5438957.0` y `5438957` son el mismo producto, asi que un campo numerico o
   unos ceros a la izquierda no dejan al pedido sin stock.
   **Solo entra el ultimo corte**: el stock es una foto, no un acumulado. Si la
   fuente trae historico, los cortes anteriores se descartan y la app informa
   cuantas filas dejo fuera.
   **El disponible es neto**: a cada almacen se le descuentan sus columnas de
   reserva, reconocidas por el encabezado (cualquiera que diga `reserva`). En
   una bodega central cuenta su `stock_bodega`; en una tienda fisica, su
   `stock_tiendas`. Nunca se suman los dos: es la misma mercaderia contada dos
   veces. El corte se decide **por dia**: si el origen sella
   cada lote con su propia hora, todas las horas de ese dia siguen siendo la
   misma foto. Las filas repetidas de un mismo par (SKU, tienda) con la misma
   marca de tiempo se suman, nunca se pisan; si el par trae **dos** marcas de
   tiempo del mismo dia, gana la mas nueva (son dos fotos, no dos almacenes).
   La app muestra, SKU por SKU, si la fuente no lo devolvio (`SIN RESPUESTA`),
   si lo devolvio en cero (`EN CERO`) o con unidades (`CON STOCK`); el detalle
   tambien viaja en el reporte operativo.
4. Reasigna segun una **lista de prioridad configurable en Excel**, nunca en codigo.
   La prioridad viene en bandas con empates, y dentro de una banda gana la tienda
   con **mas stock**.
5. **Cuida a la tienda que cede**: prefiere una que conserve unidades despues de
   ceder, en vez de vaciar la que tiene poco. Solo si ninguna puede, acepta la
   ultima unidad, y lo deja anotado en `Reasig_Detalle`.
6. Descuenta el stock utilizado durante la misma corrida, para no comprometer dos
   veces el mismo par (SKU, tienda).
7. Nunca reasigna a la tienda de origen del propio pedido.
8. Marca `SIN OPCION DE REASIGNACION` cuando ninguna tienda alcanza.
8b. Informa una **2da opcion**: la siguiente tienda de la prioridad que tambien
   podria cubrir el pedido entero. Sirve para cuando la tienda elegida ya
   rechazo el pedido y hay que reasignarlo a mano sin volver a correr la app.
   **No se le reserva stock** — reservarlo dejaria unidades sin usar en cada
   corrida —, por eso se informa cuantas unidades tenia al momento del
   calculo: si dice 1, esa unidad la puede tomar un solo pedido.
9. Genera el Excel final con las columnas originales intactas y la tienda escrita
   en `Nom Tda Reasignada` (la crea si el archivo no la trae).

Antes de programar nada se analizo un archivo real de 402 pedidos y 58 columnas:
ver **[docs/ANALISIS_EXCEL.md](docs/ANALISIS_EXCEL.md)** para columnas, estados,
riesgos detectados y el diseno de la tabla de prioridades.

---

## Instalacion

```bash
git clone <url-del-repositorio>
cd reassign-control-center
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
```

Configura los secretos:

```bash
copy .streamlit\secrets.example.toml .streamlit\secrets.toml
```

### Compatible con los secrets de Catalogo Control Center

Puedes pegar tal cual los bloques `[bigquery]`, `[gcp_service_account]` y
`[app_auth]` de esa aplicacion: las claves que esta app no usa se ignoran, y el
login acepta el mismo formato (`username`/`password` o `[app_auth.users]`).

Una advertencia que importa: **en Catalogo Control Center la clave `table`
apunta a la tabla ARTI** (maestro de productos), no al stock. Esta app la
**ignora a proposito** y resuelve el stock asi:

| Clave | Uso |
|-------|-----|
| `stock_table` | tabla de stock, si quieres fijar otra |
| *(ausente)* | `forus-analitica-prod-datalake.bronze.stg_pe_central_stock_bi` |
| `table` | **ignorada** (es la tabla ARTI) |

Tambien acepta un `stock_query` propio. Si trae el parametro `@skus` se consulta
por lotes; si no, se ejecuta completa una vez y se filtra en memoria. Las columnas
pueden venir como `sku`/`id_producto` y `cod_tienda`/`codigo_tienda`.

La seccion `[app_auth]` es **opcional**: si no existe, la app queda sin login.

Genera la configuracion de prioridad y arranca:

```bash
python -m scripts.build_priority_template
python -m streamlit run app.py
```

**La prioridad ya viene configurada.** `config/prioridad_tiendas.xlsx` viaja
versionado en el repositorio, asi que la app desplegada la carga sola y no pide
subir nada. Para cambiarla: edita ese archivo y haz commit, o subela desde la
barra lateral solo para esa sesion.

---

## Configuracion de prioridad (no se toca codigo)

Todo el criterio de negocio vive en `config/prioridad_tiendas.xlsx`, editable desde
Excel por el equipo comercial. Se puede tambien subir desde la barra lateral sin
tocar el servidor.

| Hoja | Contenido |
|------|-----------|
| `Prioridad` | `sitio`, `marca`, `cod_tienda`, `nom_tienda`, `prioridad`, `activo`, `stock_seguridad`, `max_unidades` |
| `Tiendas` | maestro de bodegas: codigo, nombre, activo, stock de seguridad |
| `Parametros` | reglas conmutables del motor (ver tabla abajo) |
| `Instrucciones` | ayuda para quien edita el archivo |

`sitio` y `marca` aceptan `*` como comodin. **Una fila con el sitio exacto siempre
desplaza a las filas comodin**, asi el resultado es predecible.

### Como se elige la tienda

1. **Banda de prioridad** del area comercial: la 1 antes que la 2, y asi.
2. **Dentro de la banda**, la tienda con mas stock (`ordenar_por_stock`). Importa
   porque las bandas traen empates: la lista real tiene 26 tiendas en la banda 11.
3. **Reserva**: en la primera pasada solo entran tiendas que quedarian con al
   menos `reserva_por_tienda` unidades. Si ninguna de la lista puede, recien ahi
   se acepta dejar una tienda en cero, y la fila lo declara en `Reasig_Detalle`
   ("Ultimo recurso...").

Ambos valores se ajustan tambien desde la interfaz, en el paso de reasignacion.

### Importar la lista del area comercial

Si te mandan un Excel simple de una hoja (`ID Tienda | nombre | Prioridad`), la app
lo lee tal cual. Para convertirlo al formato de 3 hojas y poder tocar los
parametros:

```bash
python -m scripts.import_priority "ruta\Priorizacion Tiendas.xlsx"
```

### Parametros

| Parametro | Default | Efecto |
|-----------|---------|--------|
| `estados_objetivo` | `SIN_STOCK,SIN_DESPACHO` | que estados se reasignan |
| `excluir_tienda_origen` | `SI` | no reasignar a la tienda que ya tenia el pedido |
| `permitir_reasignacion_parcial` | `NO` | cubrir solo parte de las unidades |
| `agrupar_por_shgroup` | `NO` | todas las lineas de un despacho a la misma tienda |
| `fallback_linea_si_grupo_falla` | `SI` | si nadie cubre el grupo, resolver linea por linea |
| `incluir_stock_bodega_central` | `SI` | en las bodegas centrales suma `stock_bodega` |
| `codigos_bodega_central` | `320` | que bodegas cuentan como centrales, separadas por coma |
| `formula_bodega_central` | `solo_bodega` | `solo_bodega`, `sumar` o `restar_tiendas` en una bodega central |
| `stock_seguridad_global` | `0` | unidades intocables en todas las tiendas |
| `reserva_por_tienda` | `1` | unidades que la tienda deberia conservar tras ceder; `0` desactiva |
| `ordenar_por_stock` | `SI` | dentro de la misma banda gana la tienda con mas stock |
| `opciones_alternativas` | `1` | tiendas de respaldo que se informan por pedido (0 a 3) |
| `max_unidades_por_tienda` | `0` | tope por tienda y corrida (`0` = sin tope) |
| `columna_salida` | `Nom Tda Reasignada` | nombre exacto de la columna destino |

---

## Interfaz

Capturas de la aplicacion en ejecucion, en `capturas/`:

| Archivo | Pantalla |
|---------|----------|
| `capturas/1-acceso.png` | Acceso |
| `capturas/2-validacion.png` | Validacion del archivo (paso 2) |
| `capturas/3-resultado.png` | KPIs, vista previa y descargas (paso 5) |

Las mismas pantallas en HTML responsive: **[docs/preview.html](docs/preview.html)**.

**Pantalla de acceso**: se activa configurando `[app_auth]` en `secrets.toml`.
Sin esa seccion la app queda abierta, para que un despliegue nuevo no se bloquee
antes de estar configurado.

Barra lateral **permanente** (no colapsable) con marca, rail de pasos, configuracion
de prioridad y fuente de stock.

**Selector de estados**: en el paso 2 se marcan los estados a reasignar entre los
que realmente trae el archivo, sin editar ningun Excel. El resto de las filas se
conservan intactas en la salida.

**Nombres de columna tolerantes**: reconoce `Sitio_1` (sufijo que agrega Excel al
duplicar), `Método_de_Despacho` con tilde y `MÃ©todo_de_Despacho` con la
codificacion danada. El encabezado original se conserva byte a byte en la salida.

**KPIs**: pedidos recibidos, pedidos a reasignar, reasignados, sin stock disponible,
errores; mas unidades, tiendas usadas y tasa de exito.

**Vista previa**: `Pedido | SKU | Tienda origen | Unidades | Tienda reasignada |
Stock disponible | Resultado`, con buscador y pestanas por resultado, por tienda y
de validacion.

**Verificacion antes de descargar**: la app audita su propio resultado
recontando el stock consumido desde el detalle. Si algo no cuadra (sobre-uso de
stock, tienda fuera de prioridad, columna perdida, indicadores descuadrados) el
Excel no se genera y se explica el motivo.

**Descargas**:

| Archivo | Para que |
|---------|----------|
| `reasignacion_<fecha>.xlsx` | el que se sube a la plataforma destino |
| `reporte_reasignacion_<fecha>.xlsx` | interno: KPIs, detalle, uso por tienda, validaciones, prioridad usada |

Las columnas de trazabilidad (`Reasig_*`) son **opcionales**: desactivadas, el Excel
de carga queda con exactamente las columnas originales mas la de reasignacion.
Entre ellas viajan `Reasig_2da_Opcion`, `Reasig_Cod_2da_Opcion` y
`Reasig_Stock_2da_Opcion` con la tienda de respaldo; en el reporte operativo las
mismas columnas salen como `2da opcion` y `Stock 2da opcion`.

---

## Cuando la app no trae stock

El paso 4 ya separa, SKU por SKU, el que la fuente no conoce (`SIN RESPUESTA`)
del que vuelve en cero (`EN CERO`). Si no vuelve **ni una fila**, el paso 3
muestra ademas el boton **"Diagnosticar fuente de stock"**, que corre
contadores de solo lectura sobre la tabla y separa causas que en pantalla se
ven identicas:

| Lo que informa | Que significa |
|----------------|---------------|
| Filas en la tabla = 0 | la tabla de stock esta vacia: es el origen, no la app |
| Filas del ultimo corte = 0 | hay una `fecha_corte` nueva pero sin datos (carga a medio terminar) |
| SKU hallados en la tabla = 0 | los SKU del archivo no existen en la tabla de stock |
| SKU en el ultimo corte = 0 | existen, pero solo en cortes viejos: el stock no usa fechas pasadas |

El diagnostico canoniza el SKU con la misma regla que la consulta real, para
que sus contadores sean comparables con lo que devuelve el paso 3.

Dos detalles del origen que hacian que la consulta devolviera **cero filas sin
fallar**, y que la app ahora resuelve sola:

* `fecha_corte` como marca de tiempo. `MAX(fecha_corte)` es un **instante**: si
  el ETL sella cada lote con su hora, unir por igualdad se queda con una
  rebanada minima de la foto. Ahora se une por dia.
* Offsets UTC mezclados en `fecha_corte`. Al compararlos como fecha, pandas
  lanzaba `Mixed timezones detected` y tumbaba la consulta entera.

---

## Modo sin BigQuery

En la barra lateral se puede elegir **"Archivo de stock"** y subir un Excel/CSV con
al menos `sku`, `cod_tienda` y `stock`. Sirve para probar la app sin credenciales y
para simulaciones puntuales del equipo.

---

## Estructura

```
app.py                          entrypoint Streamlit (flujo de 6 pasos)
config/
  settings.py                   columnas, rutas, defaults (sin reglas de negocio)
  prioridad_tiendas.xlsx        prioridad de tiendas (versionada)
core/
  excel_io.py                   lectura/escritura fiel de .xls y .xlsx
  validation.py                 validaciones y deteccion de riesgos
  priority.py                   carga y resolucion de la lista de prioridad
  stock_source.py               BigQuery (solo lectura) y archivo de stock
  engine.py                     motor de reasignacion y KPIs
ui/
  theme.py                      tema Forus + barra lateral permanente
  components.py                 hero, KPIs, notas, rail de pasos
scripts/
  build_priority_template.py    genera la plantilla de configuracion
  import_priority.py            convierte la lista simple del area comercial
  build_release_zip.py          empaqueta el proyecto para GitHub
  test_rules.py                 29 pruebas de reglas de negocio
  test_stock_ledger.py          11 pruebas del descuento temporal de stock
  test_stock_cutoff.py          15 pruebas del filtro por fecha de corte
  test_stock_match.py           16 pruebas del cruce SKU <-> stock
  test_app_flow.py              15 pruebas de la app (acceso, flujo, pasos 3-5)
  test_secrets_compat.py        11 pruebas de compatibilidad de secrets
  smoke_test.py                 prueba end-to-end contra un Excel real
docs/
  ANALISIS_EXCEL.md             analisis del formato de entrada
  preview.html                  vista previa de las pantallas
capturas/                       PNG de la app en ejecucion
assets/forus.png                logotipo institucional
```

No usa FastAPI: es una aplicacion Streamlit autocontenida.

---

## Pruebas

```bash
python -m scripts.test_rules
```

29 casos sobre el motor. Cubre prioridad, reserva de stock por tienda,
desempate por profundidad de stock, descuento temporal de stock, no-sobreventa, exclusion de tienda
origen, `SIN OPCION DE REASIGNACION`, filtro de estados, stock de seguridad, topes
por tienda, reasignacion parcial, agrupacion por `ShGroup`, creacion de la columna
de salida, reconocimiento de columnas con tilde/mojibake/sufijo y manejo de
filas sin SKU.

```bash
python -m scripts.test_stock_ledger
```

11 casos sobre el descuento temporal, la parte critica. Defienden una sola
invariante: para todo par (SKU, tienda), la suma de unidades reasignadas nunca
supera el stock inicial de BigQuery. Incluyen varias ordenes del mismo SKU,
ordenes de varias unidades, tiendas que llegan a cero y una carga de 300
ordenes contra 40 unidades.

```bash
python -m scripts.test_stock_cutoff
```

15 casos sobre el filtro de fecha: historico de dos anios, fechas `DD/MM/YYYY`
(donde comparar como texto elige mal), fechas con hora, consultas propias sin
filtro de fecha, marcas de tiempo distintas por lote dentro del mismo dia,
offsets UTC mezclados y la canonizacion del SKU en la propia consulta.

```bash
python -m scripts.test_stock_match
```

16 casos sobre el cruce entre el SKU del pedido y el stock, que es donde un
"sin stock" puede ser en realidad un codigo que no cruza: ceros a la izquierda
en cualquiera de los dos lados, el `.0` de un campo numerico, codigos
alfanumericos que **si** conservan su cero, filas repetidas del mismo par
(SKU, tienda) que deben sumarse, bodegas centrales, y el detalle que separa
"no vino en la consulta" de "vino en cero".

```bash
python -m scripts.test_app_flow
```

15 casos sobre la aplicacion con `streamlit.testing.AppTest`: pantalla de acceso,
credenciales correctas e incorrectas, normalizacion del correo, cierre de sesion,
arranque del flujo, los KPI del paso 4, el detalle de stock por SKU, el descarte
de una consulta de stock que ya no corresponde y la corrida completa hasta el
boton de descarga.

```bash
python -m scripts.test_secrets_compat
```

11 casos que fijan la compatibilidad con los secrets de Catalogo Control Center:
que `table` (tabla ARTI) nunca se use como tabla de stock, que la service account
se acepte como dict o como JSON en texto, y que una consulta con el esquema de
`stg_pe_central_stock_bi` se entienda igual que una propia.

```bash
python -m scripts.smoke_test "ruta\al\Formato de Carga Reasignacion.xls"
```

Corre el flujo completo con stock sintetico y verifica que no haya sobreasignacion,
que ninguna fila vuelva a su tienda origen y que el archivo escrito conserve las 58
columnas originales.

---

## Notas de operacion

* **`ShGroup` de mas de 15 digitos**: Excel los guarda como coma flotante y los
  ultimos digitos ya vienen alterados desde el origen. La app los escribe como texto
  para no empeorarlo, pero conviene pedirlos como texto al sistema que genera el
  archivo.
* **`Nom_Tienda_Asig` vacio**: en el archivo de muestra lo esta en el 94 % de las
  filas, asi que la regla de excluir la tienda origen aplica a muy pocos casos. La
  app lo reporta como alerta en el paso 2.
* **Encabezados con mojibake** (`Â¿Quien_Recibe?`): se conservan byte a byte porque
  la plataforma destino los espera asi.
* **`client.showSidebarNavigation = false`**: no activarlo. En Streamlit 1.60 esa
  opcion elimina la barra lateral completa en apps de una sola pagina.

---

## Empaquetar para GitHub

```bash
python -m scripts.build_release_zip
```

Genera `reassign-control-center.zip` dejando fuera `secrets.toml`, la configuracion
operativa de prioridad, `outputs/` y los caches. Aborta si detecta un secreto dentro
del paquete.

---

## Despliegue

**Streamlit Community Cloud**: apuntar a `app.py`, pegar el contenido de
`secrets.example.toml` en *Settings -> Secrets* con los valores reales, y subir
`config/prioridad_tiendas.xlsx` por la barra lateral (o versionarlo si no es
sensible).

`.streamlit/secrets.toml` y `config/prioridad_tiendas.xlsx` estan en `.gitignore`:
las credenciales y la configuracion operativa nunca van al repositorio.
