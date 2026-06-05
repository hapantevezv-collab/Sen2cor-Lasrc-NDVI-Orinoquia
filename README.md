# NDVI - Sen2Cor v0.7.0

Plugin QGIS para calcular series temporales de NDVI Sentinel-2 mediante Google Earth Engine. Esta versión simplifica la interfaz para reducir errores de parametrización: la colección base se selecciona con etiquetas cortas y la corrección radiométrica manual aparece únicamente cuando se trabaja con Sentinel-2 TOA/L1C.

## Cambios principales de v0.7.0

1. La lista **Colección NDVI base** ahora usa etiquetas cortas:
   - `sen2cor_sr | Sen2Cor SR`
   - `toa_l1c | Sentinel-2 TOA/L1C`
2. La sección **Corrección radiométrica TOA/L1C (manual)** solo se muestra cuando la colección base seleccionada es `toa_l1c`.
3. Se eliminó de la interfaz la selección de múltiples modos de corrección para evitar confusión operativa. La corrección local se aplica únicamente como valor manual aditivo:

   `NDVI_corr = NDVI_TOA + δ_manual`

4. El campo **Años TOA a corregir** queda asociado al rango temporal consultado. Acepta formatos como:
   - `2018`
   - `2015-2019`
   - `2015,2016,2017,2018,2019`

   Si se ingresan años fuera del rango de consulta, el plugin los ignora y reporta la advertencia en el log.

5. Se agregó la opción **Directorio de salida**, para escoger explícitamente dónde se exportan los archivos locales:
   - serie AOI CSV,
   - estadísticos por polígono CSV,
   - serie polígono × fecha CSV,
   - GeoJSON de polígonos,
   - gráfico PNG.

## Uso recomendado

### Serie Sen2Cor SR sin corrección

Use esta configuración cuando la serie proviene de reflectancia de superficie Sen2Cor:

```text
Colección NDVI base: sen2cor_sr | Sen2Cor SR
Corrección radiométrica: oculta / no aplica
```

En este caso el backend ejecuta `correction_mode = none`.

### Serie TOA/L1C con corrección manual

Use esta configuración cuando trabaje con Sentinel-2 TOA/L1C y quiera aplicar un factor manual definido por la investigación o por una prueba de sensibilidad:

```text
Colección NDVI base: toa_l1c | Sentinel-2 TOA/L1C
Años TOA a corregir: 2015-2019
Valor δ manual: 0.320 NDVI
```

El plugin aplicará el valor manual únicamente a los años indicados y dentro del rango temporal consultado. Las salidas conservan trazabilidad mediante columnas `ndvi_<stat>_raw`, `ndvi_<stat>_correction`, `radiometric_correction_mode`, `radiometric_correction_year` y `radiometric_correction_applied`.

## Nota metodológica

En esta versión el plugin no selecciona automáticamente factores por año desde la tabla consolidada. El operador debe ingresar el valor manual que corresponda al ejercicio metodológico que desea ejecutar. Para aplicar valores diferentes por año, ejecute corridas separadas por año o por subconjunto de años con el valor δ correspondiente.

## Instalación

Copie la carpeta `ndvi_sen2cor` en el directorio de plugins de QGIS, por ejemplo:

```text
C:\Users\Lenovo\AppData\Roaming\QGIS\QGIS3\profiles\default\python\plugins\ndvi_sen2cor
```

Luego reinicie QGIS y active el complemento desde el administrador de complementos.

## Configuración GEE

En el panel del plugin indique:

- Python del entorno externo con `earthengine-api`.
- Ruta del backend `gee_ndvi_backend.py` incluido en esta carpeta.
- Google Cloud Project ID, por ejemplo `ee-hapantevezv`.
- Directorio local donde se guardarán las salidas.
