-- Diagnostico del stock para los 19 SKU de shipping_groups_1788381393171.xlsx
-- Pegar tal cual en la consola de BigQuery. Es SOLO LECTURA.
--
-- La fila que devuelve responde, de una vez, todas las hipotesis abiertas.

DECLARE skus ARRAY<STRING> DEFAULT [
    '3258366',
    '3363166',
    '4548388',
    '4882595',
    '5060721',
    '5071606',
    '5271189',
    '5304106',
    '5312624',
    '5315197',
    '5330876',
    '5455571',
    '5468235',
    '5470513',
    '5479683',
    '5504453',
    '5511064',
    '5532571',
    '5542122'
];

WITH base AS (
  SELECT
    REGEXP_REPLACE(UPPER(TRIM(CAST(s.id_producto AS STRING))), r'[.]0+$', '') AS limpio,
    CAST(s.id_producto AS STRING) AS crudo,
    s.fecha_corte                 AS fecha_corte,
    COALESCE(SAFE_CAST(CAST(s.fecha_corte AS STRING) AS DATE), SAFE_CAST(LEFT(CAST(s.fecha_corte AS STRING), 10) AS DATE))                         AS dia_corte,
    COALESCE(SAFE_CAST(CAST(s.stock_tiendas AS STRING) AS FLOAT64), 0) AS unidades
  FROM `forus-analitica-prod-datalake.bronze.stg_pe_central_stock_bi` AS s
),
canon AS (
  SELECT IF(REGEXP_CONTAINS(limpio, r'^[0-9]+$'), IFNULL(REGEXP_EXTRACT(limpio, r'^0*([0-9]+?)$'), limpio), limpio) AS sku, crudo, fecha_corte, dia_corte, unidades FROM base
),
topes AS (
  SELECT MAX(dia_corte) AS ultimo_dia, MAX(fecha_corte) AS ultimo_instante FROM canon
)
SELECT
  (SELECT COUNT(*) FROM canon)                                    AS filas_tabla,
  (SELECT CAST(ultimo_dia AS STRING) FROM topes)                  AS ultimo_dia,
  (SELECT CAST(ultimo_instante AS STRING) FROM topes)             AS ultimo_instante,

  -- CLAVE: si estos dos numeros son MUY distintos, el corte por instante
  -- (lo que hace main hoy) esta descartando casi toda la foto del dia.
  (SELECT COUNT(*) FROM canon c, topes t WHERE c.dia_corte   = t.ultimo_dia)      AS filas_en_el_dia,
  (SELECT COUNT(*) FROM canon c, topes t WHERE c.fecha_corte = t.ultimo_instante) AS filas_en_el_instante,

  -- Cruce de los 19 SKU del archivo
  (SELECT COUNT(DISTINCT sku)   FROM canon WHERE sku   IN UNNEST(skus))           AS skus_canonizados,
  (SELECT COUNT(DISTINCT crudo) FROM canon WHERE crudo IN UNNEST(skus))           AS skus_crudos,
  (SELECT COUNT(DISTINCT c.sku) FROM canon c, topes t
     WHERE c.dia_corte = t.ultimo_dia   AND c.sku IN UNNEST(skus))                AS skus_en_el_dia,
  (SELECT COUNT(DISTINCT c.sku) FROM canon c, topes t
     WHERE c.fecha_corte = t.ultimo_instante AND c.sku IN UNNEST(skus))           AS skus_en_el_instante,
  (SELECT COUNT(DISTINCT c.sku) FROM canon c, topes t
     WHERE c.dia_corte = t.ultimo_dia AND c.sku IN UNNEST(skus) AND c.unidades > 0) AS skus_con_unidades
