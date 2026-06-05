# -*- coding: utf-8 -*-
"""
NDVI - Sen2Cor - controlador del plugin QGIS (v0.7.0)

Arquitectura de dos entornos:
  - El backend GEE (gee_ndvi_backend.py) corre en un entorno externo con
    earthengine-api.
  - Este plugin QGIS NO importa ee. Invoca el backend como subproceso y parsea
    su salida JSON.

Correcciones v0.4.0/v0.4.1:
  1) La serie temporal AOI respeta el estadístico seleccionado.
  2) La gráfica no conecta duplicados de fecha sin agregarlos.
  3) La tendencia suavizada no usa np.convolve(mode="same"), evitando caídas
     artificiales en bordes.
  4) El eje Y y el título son dinámicos y metodológicamente coherentes.
  5) El GeoJSON de salida se simboliza por NDVI en QGIS.
  6) Se usa carpeta de configuración de QGIS, no la carpeta del plugin.
  7) Se corrige la instrucción de autenticación con un subcomando existente.
  8) La opción de exportación a Drive se pasa al backend.
  9) Backend v0.4.1 evita User memory limit exceeded en composite=none para AOI complejos.
  10) v0.4.2 corrige la visualización: cuando hay varios estadísticos, el gráfico muestra el rango min–max, media, mediana y DE, no solo la primera casilla seleccionada.
"""

import json
import os
import subprocess
import tempfile
import time
from collections import defaultdict
from datetime import datetime

from qgis.PyQt.QtCore import QDate
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.PyQt.QtWidgets import (
    QAction,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QButtonGroup,
    QDateEdit,
    QDoubleSpinBox,
)

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeatureRequest,
    QgsGeometry,
    QgsGraduatedSymbolRenderer,
    QgsJsonExporter,
    QgsMessageLog,
    QgsProject,
    QgsRectangle,
    QgsRendererRange,
    QgsSymbol,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand

PLUGIN_DIR = os.path.dirname(__file__)
BACKEND_NAME = "gee_ndvi_backend.py"

SETTINGS_DIR = os.path.join(QgsApplication.qgisSettingsDirPath(), "ndvi_sen2cor")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "gee_settings.json")

STAT_LABELS = {
    "mean": "Media",
    "median": "Mediana",
    "min": "Mínimo",
    "max": "Máximo",
    "stdDev": "Desv. estándar",
}

STAT_Y_LABELS = {
    "mean": "NDVI medio (AOI)",
    "median": "NDVI mediano (AOI)",
    "min": "NDVI mínimo (AOI)",
    "max": "NDVI máximo (AOI)",
    "stdDev": "Desviación estándar del NDVI (AOI)",
}


# ============================================================
#  Herramienta de mapa: dibujar rectángulo AOI
# ============================================================

class RectangleTool(QgsMapToolEmitPoint):
    """Captura dos clics y define un rectángulo como AOI."""

    def __init__(self, canvas, on_done):
        super().__init__(canvas)
        self.canvas = canvas
        self.on_done = on_done
        self.start = None
        self.rb = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.rb.setColor(QColor(46, 125, 50, 170))
        try:
            self.rb.setFillColor(QColor(46, 125, 50, 35))
        except Exception:
            pass
        self.rb.setWidth(2)

    def canvasPressEvent(self, event):
        point = self.toMapCoordinates(event.pos())
        if self.start is None:
            self.start = point
            self.rb.reset(QgsWkbTypes.PolygonGeometry)
        else:
            rect = QgsRectangle(self.start, point)
            self.start = None
            self.on_done(rect)

    def canvasMoveEvent(self, event):
        if self.start is None:
            return
        point = self.toMapCoordinates(event.pos())
        rect = QgsRectangle(self.start, point)
        geom = QgsGeometry.fromRect(rect)
        self.rb.setToGeometry(geom, None)


# ============================================================
#  Diálogo principal
# ============================================================

class NdviGeeDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.aoi_rect = None
        self.map_tool = None

        self.setWindowTitle("NDVI - Sen2Cor | GEE con corrección radiométrica")
        self.setMinimumWidth(720)

        self._build_ui()
        self._on_aoi_mode(True)
        self._load_settings()

    # --------------------------------------------------------
    # UI
    # --------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)

        # 1. Configuración
        gb_cfg = QGroupBox("1. Entorno GEE y proyecto")
        cfg = QGridLayout(gb_cfg)

        cfg.addWidget(QLabel("Ejecutable Python del entorno GEE:"), 0, 0)
        self.edPy = QLineEdit()
        self.edPy.setPlaceholderText(r"Ej.: C:\...\gee_env\.pixi\envs\default\python.exe")
        btn_py = QPushButton("...")
        btn_py.clicked.connect(self._pick_py)
        cfg.addWidget(self.edPy, 0, 1)
        cfg.addWidget(btn_py, 0, 2)

        cfg.addWidget(QLabel("Backend gee_ndvi_backend.py:"), 1, 0)
        self.edBackend = QLineEdit()
        btn_backend = QPushButton("...")
        btn_backend.clicked.connect(self._pick_backend)
        cfg.addWidget(self.edBackend, 1, 1)
        cfg.addWidget(btn_backend, 1, 2)

        cfg.addWidget(QLabel("Google Cloud Project ID:"), 2, 0)
        self.edProject = QLineEdit()
        self.edProject.setPlaceholderText("Ej.: ee-tuusuario")
        cfg.addWidget(self.edProject, 2, 1, 1, 2)
        layout.addWidget(gb_cfg)

        # 2. Autenticación
        gb_auth = QGroupBox("2. Autenticación")
        auth_lay = QVBoxLayout(gb_auth)
        auth_row = QHBoxLayout()

        self.btnAuthUrl = QPushButton("Autenticar GEE ahora")
        self.btnAuthUrl.clicked.connect(self._auth_url)
        self.btnCheck = QPushButton("Verificar conexión")
        self.btnCheck.clicked.connect(self._check)

        auth_row.addWidget(self.btnAuthUrl)
        auth_row.addWidget(self.btnCheck)
        auth_lay.addLayout(auth_row)

        self.lblAuth = QLabel("Estado: sin verificar.")
        self.lblAuth.setWordWrap(True)
        auth_lay.addWidget(self.lblAuth)
        layout.addWidget(gb_auth)

        # 3. AOI
        gb_aoi = QGroupBox("3. Área de interés (AOI)")
        aoi_lay = QVBoxLayout(gb_aoi)

        self.rbDraw = QRadioButton("Dibujar rectángulo en el mapa")
        self.rbDraw.setChecked(True)
        self.rbLayer = QRadioButton("Usar capa de polígonos")

        self._aoi_group = QButtonGroup(self)
        self._aoi_group.setExclusive(True)
        self._aoi_group.addButton(self.rbDraw)
        self._aoi_group.addButton(self.rbLayer)
        self.rbDraw.toggled.connect(self._on_aoi_mode)

        aoi_lay.addWidget(self.rbDraw)
        aoi_lay.addWidget(self.rbLayer)

        draw_row = QHBoxLayout()
        self.btnDraw = QPushButton("Activar dibujo de AOI")
        self.btnDraw.clicked.connect(self._activate_draw)
        self.lblAoi = QLabel("AOI: ninguna")
        draw_row.addWidget(self.btnDraw)
        draw_row.addWidget(self.lblAoi)
        aoi_lay.addLayout(draw_row)

        layer_row = QHBoxLayout()
        self.cbLayer = QComboBox()
        self.cbLayer.currentIndexChanged.connect(self._on_layer_changed)
        layer_row.addWidget(QLabel("Capa:"))
        layer_row.addWidget(self.cbLayer)
        aoi_lay.addLayout(layer_row)

        field_row = QHBoxLayout()
        self.cbField = QComboBox()
        self.cbField.currentIndexChanged.connect(self._on_field_changed)
        self.cbCategory = QComboBox()
        field_row.addWidget(QLabel("Campo:"))
        field_row.addWidget(self.cbField)
        field_row.addWidget(QLabel("Categoría:"))
        field_row.addWidget(self.cbCategory)
        aoi_lay.addLayout(field_row)

        self._refresh_layers()
        layout.addWidget(gb_aoi)

        # 4. Parámetros
        gb_params = QGroupBox("4. Rango temporal, filtros y estadísticos")
        params = QGridLayout(gb_params)

        self.dStart = QDateEdit(QDate(2019, 6, 1))
        self.dStart.setCalendarPopup(True)
        self.dEnd = QDateEdit(QDate(2019, 9, 30))
        self.dEnd.setCalendarPopup(True)

        params.addWidget(QLabel("Fecha inicio:"), 0, 0)
        params.addWidget(self.dStart, 0, 1)
        params.addWidget(QLabel("Fecha fin:"), 0, 2)
        params.addWidget(self.dEnd, 0, 3)

        self.spCloud = QDoubleSpinBox()
        self.spCloud.setRange(0, 100)
        self.spCloud.setValue(20)
        self.spCloud.setSuffix(" %")

        self.spScale = QDoubleSpinBox()
        self.spScale.setRange(10, 1000)
        self.spScale.setValue(10)
        self.spScale.setSuffix(" m")

        params.addWidget(QLabel("Nubes máx. Sentinel-2:"), 1, 0)
        params.addWidget(self.spCloud, 1, 1)
        params.addWidget(QLabel("Escala:"), 1, 2)
        params.addWidget(self.spScale, 1, 3)

        params.addWidget(QLabel("Compuesto temporal:"), 2, 0)
        self.cbComposite = QComboBox()
        self.cbComposite.addItems([
            "none (mosaico diario de imágenes disponibles)",
            "monthly (compuesto mensual)",
            "yearly (compuesto anual)",
        ])
        params.addWidget(self.cbComposite, 2, 1, 1, 3)

        params.addWidget(QLabel("Colección NDVI base:"), 3, 0)
        self.cbInputCollection = QComboBox()
        self.cbInputCollection.addItems([
            "sen2cor_sr | Sen2Cor SR",
            "toa_l1c | Sentinel-2 TOA/L1C",
        ])
        self.cbInputCollection.currentIndexChanged.connect(self._on_input_collection_changed)
        params.addWidget(self.cbInputCollection, 3, 1, 1, 3)

        self.dStart.dateChanged.connect(self._refresh_correction_year_hint)
        self.dEnd.dateChanged.connect(self._refresh_correction_year_hint)

        params.addWidget(QLabel("Estadísticos NDVI:"), 4, 0)
        stats_row = QHBoxLayout()
        self.ckStats = {}
        for name, label in [
            ("mean", "Media"),
            ("median", "Mediana"),
            ("min", "Mínimo"),
            ("max", "Máximo"),
            ("stdDev", "Desv.est"),
        ]:
            cb = QCheckBox(label)
            cb.setChecked(name == "mean")
            self.ckStats[name] = cb
            stats_row.addWidget(cb)

        stats_widget = QWidget()
        stats_widget.setLayout(stats_row)
        params.addWidget(stats_widget, 4, 1, 1, 3)

        self.ckPolyTs = QCheckBox("Generar también serie por polígono x fecha")
        params.addWidget(self.ckPolyTs, 5, 0, 1, 4)

        self.chkExport = QRadioButton("Exportar compuesto NDVI a Google Drive")
        self.chkExport.setAutoExclusive(False)
        params.addWidget(self.chkExport, 6, 0, 1, 4)

        layout.addWidget(gb_params)

        # 5. Corrección radiométrica manual, visible únicamente para TOA/L1C
        self.gbCorrection = QGroupBox("5. Corrección radiométrica TOA/L1C (manual)")
        corr = QGridLayout(self.gbCorrection)

        self.lblCorrectionYears = QLabel("Años TOA a corregir:")
        corr.addWidget(self.lblCorrectionYears, 0, 0)
        self.edCorrectionYears = QLineEdit()
        self.edCorrectionYears.setPlaceholderText("Ej.: 2015-2019 o 2015,2016,2017")
        corr.addWidget(self.edCorrectionYears, 0, 1)

        self.lblManualCorrection = QLabel("Valor δ manual:")
        corr.addWidget(self.lblManualCorrection, 0, 2)
        self.spManualCorrection = QDoubleSpinBox()
        self.spManualCorrection.setRange(-1.0, 1.0)
        self.spManualCorrection.setDecimals(4)
        self.spManualCorrection.setSingleStep(0.001)
        self.spManualCorrection.setValue(0.0)
        self.spManualCorrection.setSuffix(" NDVI")
        corr.addWidget(self.spManualCorrection, 0, 3)

        self.lblCorrectionHint = QLabel(
            "Visible solo para TOA/L1C. El plugin aplicará NDVI_corr = NDVI_TOA + δ manual únicamente en los años indicados y dentro del rango consultado."
        )
        self.lblCorrectionHint.setWordWrap(True)
        corr.addWidget(self.lblCorrectionHint, 1, 0, 1, 4)
        layout.addWidget(self.gbCorrection)

        # 6. Directorio de exportación local
        gb_out = QGroupBox("6. Exportación local")
        out_lay = QGridLayout(gb_out)
        out_lay.addWidget(QLabel("Directorio de salida:"), 0, 0)
        self.edOutputDir = QLineEdit()
        self.edOutputDir.setPlaceholderText("Seleccione la carpeta donde se guardarán CSV, GeoJSON y PNG")
        btn_out = QPushButton("...")
        btn_out.clicked.connect(self._pick_output_dir)
        out_lay.addWidget(self.edOutputDir, 0, 1)
        out_lay.addWidget(btn_out, 0, 2)
        layout.addWidget(gb_out)

        # Ejecutar
        run_row = QHBoxLayout()
        self.btnRun = QPushButton("Calcular NDVI - Sen2Cor")
        self.btnRun.clicked.connect(self._run_ndvi)
        self.btnClose = QPushButton("Cerrar")
        self.btnClose.clicked.connect(self.reject)
        run_row.addWidget(self.btnRun)
        run_row.addWidget(self.btnClose)
        layout.addLayout(run_row)

        self.txtLog = QTextEdit()
        self.txtLog.setReadOnly(True)
        self.txtLog.setMinimumHeight(160)
        layout.addWidget(self.txtLog)

        self._on_input_collection_changed()

    # --------------------------------------------------------
    # Utilidades
    # --------------------------------------------------------
    def _log(self, msg):
        self.txtLog.append(str(msg))
        QgsMessageLog.logMessage(str(msg), "NDVI_SEN2COR", Qgis.Info)

    def _selected_stats(self):
        return [name for name, cb in self.ckStats.items() if cb.isChecked()] or ["mean"]

    def _selected_composite(self):
        return self.cbComposite.currentText().split()[0]

    def _selected_input_collection(self):
        text = self.cbInputCollection.currentText() if hasattr(self, "cbInputCollection") else "sen2cor_sr"
        return text.split("|")[0].strip() or "sen2cor_sr"

    def _selected_correction_mode(self):
        # v0.7.0: la corrección radiométrica local se ofrece solo para TOA/L1C
        # y siempre usa el valor manual indicado por el usuario.
        return "manual_additive" if self._selected_input_collection() == "toa_l1c" else "none"

    def _date_year_range_label(self):
        sy = self.dStart.date().year()
        ey = self.dEnd.date().year()
        return str(sy) if sy == ey else f"{sy}-{ey}"

    def _years_from_date_range(self):
        sy = self.dStart.date().year()
        ey = self.dEnd.date().year()
        lo, hi = min(sy, ey), max(sy, ey)
        return set(range(lo, hi + 1))

    def _parse_years_ui(self, text):
        years = set()
        for raw in str(text or "").replace(";", ",").replace("–", "-").replace("—", "-").split(","):
            raw = raw.strip()
            if not raw:
                continue
            if "-" in raw:
                parts = [p.strip() for p in raw.split("-") if p.strip()]
                if len(parts) == 2:
                    try:
                        a, b = int(parts[0]), int(parts[1])
                        for y in range(min(a, b), max(a, b) + 1):
                            years.add(y)
                        continue
                    except ValueError:
                        pass
            try:
                years.add(int(raw))
            except ValueError:
                pass
        return years

    def _refresh_correction_year_hint(self, *args):
        if not hasattr(self, "edCorrectionYears"):
            return
        label = self._date_year_range_label()
        self.edCorrectionYears.setPlaceholderText(f"Años consultados: {label}. Ej.: {label} o 2017,2018")
        if self._selected_input_collection() == "toa_l1c" and not self.edCorrectionYears.text().strip():
            self.edCorrectionYears.setText(label)

    def _on_input_collection_changed(self, *args):
        is_toa = self._selected_input_collection() == "toa_l1c"
        if hasattr(self, "gbCorrection"):
            self.gbCorrection.setVisible(is_toa)
        if hasattr(self, "lblCorrectionHint"):
            if is_toa:
                self.lblCorrectionHint.setText(
                    "Corrección manual activa para TOA/L1C: indique δ en unidades NDVI y los años del rango consultado a corregir. "
                    "El cálculo será NDVI_corr = NDVI_TOA + δ manual."
                )
            else:
                self.lblCorrectionHint.setText(
                    "Sin corrección: Sen2Cor SR ya corresponde a reflectancia de superficie. La sección manual queda oculta."
                )
        self._refresh_correction_year_hint()

    def _on_correction_mode_changed(self, *args):
        # Conservado para retrocompatibilidad con configuraciones antiguas.
        self._on_input_collection_changed()

    def _on_aoi_mode(self, draw_checked):
        self.btnDraw.setEnabled(draw_checked)
        self.cbLayer.setEnabled(not draw_checked)
        self.cbField.setEnabled(not draw_checked)
        self.cbCategory.setEnabled(not draw_checked)

        if not draw_checked:
            try:
                if self.map_tool is not None:
                    self.iface.mapCanvas().unsetMapTool(self.map_tool)
                    if hasattr(self.map_tool, "rb"):
                        self.map_tool.rb.reset(QgsWkbTypes.PolygonGeometry)
            except Exception:
                pass
            self.lblAoi.setText("AOI: capa seleccionada")
        else:
            self.lblAoi.setText("AOI: ninguna" if self.aoi_rect is None else self.lblAoi.text())

    def _refresh_layers(self):
        self.cbLayer.clear()
        for lyr in QgsProject.instance().mapLayers().values():
            if isinstance(lyr, QgsVectorLayer) and QgsWkbTypes.geometryType(lyr.wkbType()) == QgsWkbTypes.PolygonGeometry:
                self.cbLayer.addItem(lyr.name(), lyr.id())
        self._on_layer_changed()

    def _on_layer_changed(self, *args):
        self.cbField.blockSignals(True)
        self.cbField.clear()

        lyr = self._current_layer()
        if isinstance(lyr, QgsVectorLayer):
            self.cbField.addItem("(ninguno)")
            for field in lyr.fields():
                self.cbField.addItem(field.name())

        self.cbField.blockSignals(False)
        self._on_field_changed()

    def _on_field_changed(self, *args):
        self.cbCategory.clear()
        self.cbCategory.addItem("(todas)")

        lyr = self._current_layer()
        field = self.cbField.currentText()
        if not isinstance(lyr, QgsVectorLayer) or not field or field == "(ninguno)":
            return

        idx = lyr.fields().indexOf(field)
        if idx < 0:
            return

        values = sorted({str(v) for v in lyr.uniqueValues(idx) if v is not None})
        for value in values:
            self.cbCategory.addItem(value)

    def _current_layer(self):
        layer_id = self.cbLayer.currentData()
        return QgsProject.instance().mapLayer(layer_id) if layer_id else None

    def _pick_py(self):
        path, _ = QFileDialog.getOpenFileName(self, "Python del entorno GEE")
        if path:
            self.edPy.setText(path)

    def _pick_backend(self):
        path, _ = QFileDialog.getOpenFileName(self, "gee_ndvi_backend.py", "", "Python (*.py)")
        if path:
            self.edBackend.setText(path)

    def _pick_output_dir(self):
        path = QFileDialog.getExistingDirectory(self, "Directorio de exportación local")
        if path:
            self.edOutputDir.setText(path)

    def _settings(self):
        return {
            "py": self.edPy.text().strip(),
            "backend": self.edBackend.text().strip(),
            "project": self.edProject.text().strip(),
            "input_collection": self._selected_input_collection() if hasattr(self, "cbInputCollection") else "sen2cor_sr",
            "correction_mode": self._selected_correction_mode(),
            "correction_years": self.edCorrectionYears.text().strip() if hasattr(self, "edCorrectionYears") else "",
            "manual_correction": self.spManualCorrection.value() if hasattr(self, "spManualCorrection") else 0.0,
            "output_dir": self.edOutputDir.text().strip() if hasattr(self, "edOutputDir") else "",
        }

    def _save_settings(self):
        try:
            os.makedirs(SETTINGS_DIR, exist_ok=True)
            with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
                json.dump(self._settings(), fh, ensure_ascii=False, indent=2)
        except Exception as exc:
            self._log(f"Advertencia: no se pudo guardar configuración: {exc}")

    def _load_settings(self):
        default_backend = os.path.join(PLUGIN_DIR, BACKEND_NAME)
        try:
            if os.path.isfile(SETTINGS_FILE):
                with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
                    settings = json.load(fh)
                self.edPy.setText(settings.get("py", ""))
                self.edBackend.setText(settings.get("backend", default_backend))
                self.edProject.setText(settings.get("project", ""))
                if hasattr(self, "cbInputCollection"):
                    saved_collection = settings.get("input_collection", "sen2cor_sr")
                    for i in range(self.cbInputCollection.count()):
                        if self.cbInputCollection.itemText(i).startswith(saved_collection):
                            self.cbInputCollection.setCurrentIndex(i)
                            break
                if hasattr(self, "edCorrectionYears"):
                    self.edCorrectionYears.setText(settings.get("correction_years", ""))
                if hasattr(self, "spManualCorrection"):
                    try:
                        self.spManualCorrection.setValue(float(settings.get("manual_correction", 0.0)))
                    except Exception:
                        self.spManualCorrection.setValue(0.0)
                if hasattr(self, "edOutputDir"):
                    self.edOutputDir.setText(settings.get("output_dir", "") or tempfile.gettempdir())
                self._on_input_collection_changed()
            elif os.path.isfile(default_backend):
                self.edBackend.setText(default_backend)
                if hasattr(self, "edOutputDir"):
                    self.edOutputDir.setText(tempfile.gettempdir())
            self._on_input_collection_changed()
        except Exception as exc:
            self._log(f"Advertencia: no se pudo cargar configuración: {exc}")
            if os.path.isfile(default_backend):
                self.edBackend.setText(default_backend)
            if hasattr(self, "edOutputDir"):
                self.edOutputDir.setText(tempfile.gettempdir())
            self._on_input_collection_changed()

    def _call_backend(self, args, timeout=300):
        py = self.edPy.text().strip()
        backend = self.edBackend.text().strip()

        if not os.path.isfile(py):
            return {"ok": False, "error": "Python del entorno GEE no válido."}
        if not os.path.isfile(backend):
            return {"ok": False, "error": "Backend gee_ndvi_backend.py no encontrado."}

        cmd = [py, backend] + args
        self._log("Ejecutando: " + " ".join(f'"{c}"' if " " in c else c for c in cmd))

        env = dict(os.environ)
        for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONEXECUTABLE"):
            env.pop(key, None)

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "Tiempo de espera agotado."}

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()

        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            pass

        obj = self._extract_last_json(stdout)
        if obj is not None:
            return obj

        obj = self._extract_last_json(stderr)
        if obj is not None:
            return obj

        return {
            "ok": False,
            "error": f"Salida no-JSON. stdout: {stdout[:500]} | stderr: {stderr[:500]}",
        }

    @staticmethod
    def _extract_last_json(text):
        if not text:
            return None
        starts = [i for i, c in enumerate(text) if c == "{"]
        for start in starts:
            depth = 0
            for end in range(start, len(text)):
                if text[end] == "{":
                    depth += 1
                elif text[end] == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:end + 1]
                        try:
                            obj = json.loads(candidate)
                            if isinstance(obj, dict) and "ok" in obj:
                                return obj
                        except json.JSONDecodeError:
                            pass
                        break
        return None

    # --------------------------------------------------------
    # Autenticación
    # --------------------------------------------------------
    def _auth_url(self):
        """
        Autenticación amigable desde QGIS.

        Ejecuta el backend externo con auth_mode=localhost. QGIS no importa
        earthengine-api directamente; se conserva la arquitectura de dos entornos.
        """
        self._save_settings()

        project = self.edProject.text().strip()
        if not project:
            QMessageBox.warning(
                self,
                "Falta Project ID",
                "Indique primero el Google Cloud Project ID."
            )
            return

        py = self.edPy.text().strip()
        backend = self.edBackend.text().strip()

        if not os.path.isfile(py):
            QMessageBox.warning(
                self,
                "Python no válido",
                "La ruta del ejecutable Python del entorno GEE no es válida."
            )
            return

        if not os.path.isfile(backend):
            QMessageBox.warning(
                self,
                "Backend no encontrado",
                "La ruta de gee_ndvi_backend.py no es válida."
            )
            return

        QMessageBox.information(
            self,
            "Autenticación GEE",
            (
                "Se abrirá el navegador para autorizar Google Earth Engine.\n\n"
                "Después de aceptar los permisos, regrese a QGIS y espere el mensaje "
                "de confirmación.\n\n"
                "Si el navegador no se abre o el flujo falla, el plugin mostrará "
                "una alternativa manual con --auth-mode notebook."
            )
        )

        self.lblAuth.setText("Estado: autenticando en navegador...")
        self._log("Iniciando autenticación GEE desde QGIS con auth_mode=localhost...")

        res = self._call_backend(
            [
                "auth-url",
                "--project", project,
                "--auth-mode", "localhost"
            ],
            timeout=600
        )

        if res.get("ok"):
            self.lblAuth.setText(
                f"Estado: autenticación OK "
                f"(project={project}, ping={res.get('ping', 'OK')})."
            )
            QMessageBox.information(
                self,
                "Autenticación completada",
                (
                    "La autenticación de Google Earth Engine fue completada.\n\n"
                    "Ahora puede pulsar 'Verificar conexión' o ejecutar el cálculo NDVI."
                )
            )
            return

        error = res.get("error", "Error desconocido")
        self.lblAuth.setText("Estado: autenticación fallida.")
        self._log("ERROR autenticación: " + error)
        if res.get("hint"):
            self._log("Sugerencia: " + res["hint"])
        if res.get("trace"):
            self._log(res["trace"])

        QMessageBox.warning(
            self,
            "Autenticación fallida",
            (
                "No se pudo completar la autenticación automática desde QGIS.\n\n"
                f"Error:\n{error}\n\n"
                "Alternativa manual en PowerShell:\n\n"
                f'& "{py}" "{backend}" auth-url --project {project} --auth-mode notebook\n\n'
                "O con la CLI oficial:\n\n"
                "earthengine authenticate --force"
            )
        )

    def _check(self):
        self._save_settings()
        project = self.edProject.text().strip()
        if not project:
            QMessageBox.warning(self, "Falta Project ID", "Indique el Project ID.")
            return

        res = self._call_backend(["check", "--project", project], timeout=120)
        if res.get("ok"):
            self.lblAuth.setText(f"Estado: conexión OK (project={project}, ping={res.get('ping')}).")
        else:
            self.lblAuth.setText("Estado: sin conexión - " + res.get("error", "?"))

    # --------------------------------------------------------
    # AOI
    # --------------------------------------------------------
    def _activate_draw(self):
        canvas = self.iface.mapCanvas()
        self.map_tool = RectangleTool(canvas, self._on_rect)
        canvas.setMapTool(self.map_tool)
        self._log("Dibuje el AOI: primer clic en una esquina, segundo clic en la opuesta.")

    def _on_rect(self, rect):
        self.aoi_rect = rect
        self.lblAoi.setText(f"AOI: {rect.toString(2)}")
        self._log("AOI capturada.")

    def _json_safe_value(self, value):
        """Convierte atributos QGIS a tipos JSON serializables."""
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            return value
        try:
            json.dumps(value)
            return value
        except Exception:
            pass
        try:
            if hasattr(value, "toString"):
                return value.toString()
        except Exception:
            pass
        return str(value)

    def _clean_qgis_geometry_for_ee(self, geom, src_crs, dst_crs):
        """
        Prepara geometría QGIS antes de escribir GeoJSON para Earth Engine.

        Corrige geometrías inválidas frecuentes en capas prediales:
        - partes vacías,
        - autointersecciones simples mediante makeValid(),
        - geometrías curvas,
        - CRS distinto de EPSG:4326,
        - coordenadas Z/M, que luego el backend reduce a 2D.
        """
        if geom is None or geom.isEmpty():
            return None, "geometría vacía"

        g = QgsGeometry(geom)
        try:
            if not g.isGeosValid():
                g = g.makeValid()
        except Exception:
            try:
                g = g.makeValid()
            except Exception:
                pass

        try:
            # Convierte CurvePolygon/MultiSurface a segmentos lineales cuando aplique.
            g.convertToStraightSegment()
        except Exception:
            pass

        try:
            if src_crs != dst_crs:
                transform = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
                g.transform(transform)
        except Exception as exc:
            return None, f"falló transformación CRS a EPSG:4326: {exc}"

        try:
            if not g.isGeosValid():
                g = g.makeValid()
        except Exception:
            pass

        if g is None or g.isEmpty():
            return None, "geometría vacía después de makeValid()/transformación"

        try:
            geom_json = json.loads(g.asJson(8))
        except Exception as exc:
            return None, f"no se pudo exportar geometría como GeoJSON: {exc}"

        return geom_json, None

    def _aoi_to_geojson(self):
        """Escribe el AOI a un GeoJSON temporal en EPSG:4326, reparando geometrías."""
        dst_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        path = os.path.join(tempfile.gettempdir(), "gee_aoi.geojson")

        if self.rbDraw.isChecked():
            if self.aoi_rect is None:
                return None, "No ha dibujado un AOI."

            src_crs = QgsProject.instance().crs()
            geom = QgsGeometry.fromRect(self.aoi_rect)
            geom_json, err = self._clean_qgis_geometry_for_ee(geom, src_crs, dst_crs)
            if err:
                return None, f"AOI dibujada inválida: {err}"

            gj = {
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "properties": {"_pid": 0},
                    "geometry": geom_json,
                }],
            }
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(gj, fh, ensure_ascii=False)
            return path, None

        lyr = self._current_layer()
        if lyr is None or not lyr.isValid():
            return None, "Capa no válida."

        request = QgsFeatureRequest()
        field = self.cbField.currentText()
        category = self.cbCategory.currentText()
        if field and field != "(ninguno)" and category and category != "(todas)":
            safe_field = field.replace('"', '""')
            safe_category = category.replace("'", "''")
            request.setFilterExpression(f'"{safe_field}" = \'{safe_category}\'')

        qgis_features = list(lyr.getFeatures(request))
        if not qgis_features:
            return None, "La capa/filtro no contiene polígonos."

        out_features = []
        skipped = []
        fields = list(lyr.fields())
        src_crs = lyr.crs()

        for i, feature in enumerate(qgis_features):
            geom_json, err = self._clean_qgis_geometry_for_ee(feature.geometry(), src_crs, dst_crs)
            if err:
                skipped.append(f"feature {i}: {err}")
                continue

            props = {}
            for fld in fields:
                name = fld.name()
                try:
                    props[name] = self._json_safe_value(feature[name])
                except Exception:
                    pass
            props["_pid"] = i

            out_features.append({
                "type": "Feature",
                "properties": props,
                "geometry": geom_json,
            })

        if not out_features:
            detail = "; ".join(skipped[:5]) if skipped else "sin geometrías válidas"
            return None, "No se pudo construir un AOI válido para GEE: " + detail

        gj = {"type": "FeatureCollection", "features": out_features}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(gj, fh, ensure_ascii=False)

        if skipped:
            self._log(f"Advertencia: se omitieron {len(skipped)} geometrías inválidas del AOI. Primer caso: {skipped[0]}")

        return path, None

    # --------------------------------------------------------
    # Ejecución
    # --------------------------------------------------------
    def _run_ndvi(self):
        self._save_settings()

        project = self.edProject.text().strip()
        if not project:
            QMessageBox.warning(self, "Falta Project ID", "Indique el Project ID.")
            return

        aoi_path, err = self._aoi_to_geojson()
        if err:
            QMessageBox.warning(self, "AOI", err)
            return

        stamp = time.strftime("%Y%m%d_%H%M%S")
        out_dir = self.edOutputDir.text().strip() if hasattr(self, "edOutputDir") else ""
        if not out_dir:
            out_dir = tempfile.gettempdir()
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as exc:
            QMessageBox.warning(self, "Directorio de salida", f"No se pudo crear/acceder al directorio de salida:\n{out_dir}\n\n{exc}")
            return

        out_csv = os.path.join(out_dir, f"gee_ndvi_serie_AOI_{stamp}.csv")
        out_csv_poly = os.path.join(out_dir, f"gee_ndvi_porpoligono_{stamp}.csv")
        out_csv_polyts = os.path.join(out_dir, f"gee_ndvi_poligono_x_fecha_{stamp}.csv")
        out_geojson = os.path.join(out_dir, f"gee_ndvi_poligonos_{stamp}.geojson")

        composite = self._selected_composite()
        input_collection = self._selected_input_collection()
        stats = self._selected_stats()

        if input_collection == "toa_l1c":
            correction_mode = "manual_additive"
            correction_years = self.edCorrectionYears.text().strip() or self._date_year_range_label()
            manual_correction = self.spManualCorrection.value()
            query_years = self._years_from_date_range()
            correction_year_set = self._parse_years_ui(correction_years)
            if correction_year_set:
                outside = sorted(correction_year_set - query_years)
                inside = sorted(correction_year_set & query_years)
                if outside:
                    self._log(
                        "Advertencia: se ignorarán años de corrección fuera del rango consultado: "
                        + ",".join(str(y) for y in outside)
                    )
                    if not inside:
                        QMessageBox.warning(
                            self,
                            "Años TOA a corregir",
                            "Los años indicados para corrección no intersectan el rango temporal consultado."
                        )
                        return
                    correction_years = ",".join(str(y) for y in inside)
        else:
            correction_mode = "none"
            correction_years = ""
            manual_correction = 0.0

        ctx = {
            "stats": stats,
            "stat_plot": stats[0],
            "composite": composite,
            "input_collection": input_collection,
            "field": None,
            "category": None,
            "correction_mode": correction_mode,
            "correction_years": correction_years,
        }

        args = [
            "ndvi",
            "--project", project,
            "--aoi", aoi_path,
            "--start", self.dStart.date().toString("yyyy-MM-dd"),
            "--end", self.dEnd.date().toString("yyyy-MM-dd"),
            "--cloud", str(self.spCloud.value()),
            "--scale", str(self.spScale.value()),
            "--composite", composite,
            "--input-collection", input_collection,
            "--stats", ",".join(stats),
            "--radiometric-correction-mode", correction_mode,
            "--correction-years", correction_years,
            "--manual-correction", str(manual_correction),
            "--out-csv", out_csv,
        ]

        if self.rbLayer.isChecked():
            args += ["--out-csv-poly", out_csv_poly, "--out-geojson", out_geojson]

            if self.ckPolyTs.isChecked():
                args += ["--out-csv-polyts", out_csv_polyts]

            field = self.cbField.currentText()
            if field and field != "(ninguno)":
                args += ["--field", field]
                ctx["field"] = field
                category = self.cbCategory.currentText()
                if category and category != "(todas)":
                    args += ["--category", category]
                    ctx["category"] = category

        if self.chkExport.isChecked():
            prefix = f"ndvi_{composite}_{self.dStart.date().toString('yyyyMMdd')}_{self.dEnd.date().toString('yyyyMMdd')}"
            args += [
                "--export-drive",
                "--drive-folder", "GEE_NDVI_QGIS",
                "--export-prefix", prefix,
            ]

        self._log(
            "Calculando NDVI en GEE "
            f"(colección={input_collection}, compuesto={composite}, stats={','.join(stats)}, corrección={correction_mode}, años={correction_years or 'todos'}"
            + (f", filtro {ctx['field']}={ctx['category']}" if ctx.get("category") else "")
            + ")..."
        )

        res = self._call_backend(args, timeout=1200)
        if not res.get("ok"):
            QMessageBox.critical(self, "Error GEE", res.get("error", "?"))
            self._log("ERROR: " + res.get("error", "?"))
            if res.get("trace"):
                self._log(res["trace"])
            return

        self._log(
            f"Imágenes crudas: {res.get('n_raw_images')} | "
            f"Fechas/compuestos graficables: {res.get('n_images')} | "
            f"Polígonos: {res.get('n_polys')} | compuesto: {res.get('composite')}"
        )

        if res.get("method_note"):
            self._log("Nota metodológica: " + res["method_note"])
        if res.get("radiometric_correction_note"):
            self._log("Corrección radiométrica: " + res["radiometric_correction_note"])

        series = res.get("series", [])
        self._log(f"Serie temporal AOI: {len(series)} registros -> {res.get('out_csv', out_csv)}")

        by_poly = res.get("by_poly", [])
        if by_poly:
            self._log(f"Estadísticos por polígono: {len(by_poly)} polígonos -> {res.get('out_csv_poly', out_csv_poly)}")
            stat0 = stats[0]
            key = f"ndvi_{stat0}"
            values = [row.get(key) for row in by_poly if row.get(key) is not None]
            if values:
                self._log(
                    f"  {STAT_LABELS.get(stat0, stat0)}: "
                    f"min={min(values):.4f} max={max(values):.4f} prom={sum(values)/len(values):.4f}"
                )

        if res.get("out_geojson"):
            self._log(f"GeoJSON con NDVI por polígono -> {res['out_geojson']}")
            self._load_geojson_layer(res["out_geojson"], ctx)

        if res.get("out_csv_polyts"):
            self._log(f"Serie por polígono x fecha -> {res['out_csv_polyts']}")

        if res.get("drive_export"):
            meta = res["drive_export"]
            self._log(
                "Exportación Drive lanzada: "
                f"task_id={meta.get('task_id')} | estado={meta.get('state')} | "
                f"folder={meta.get('folder')} | prefix={meta.get('prefix')}"
            )

        self._plot_series(series, out_csv, ctx)

        self.iface.messageBar().pushSuccess(
            "NDVI - Sen2Cor",
            f"Listo. Archivos exportados en: {out_dir}",
        )

    # --------------------------------------------------------
    # Carga y simbología de salida vectorial
    # --------------------------------------------------------
    def _load_geojson_layer(self, path, ctx):
        try:
            stat = ctx.get("stat_plot", "mean")
            field_name = f"ndvi_{stat}"
            name = f"NDVI {STAT_LABELS.get(stat, stat)}"
            if ctx.get("category"):
                name += f" - {ctx['field']}={ctx['category']}"

            layer = QgsVectorLayer(path, name, "ogr")
            if not layer.isValid():
                self._log("GeoJSON generado, pero la capa no es válida en QGIS.")
                return

            if layer.fields().indexOf(field_name) >= 0 and stat != "stdDev":
                ranges = []
                classes = [
                    (-1.0, 0.2, QColor("#BDBDBD"), "< 0.20 Sin vegetación / agua / suelo"),
                    (0.2, 0.4, QColor("#Fdae61"), "0.20–0.40 Vegetación baja / estresada"),
                    (0.4, 0.6, QColor("#D9EF8B"), "0.40–0.60 Vegetación moderada"),
                    (0.6, 0.8, QColor("#66BD63"), "0.60–0.80 Vegetación densa"),
                    (0.8, 1.0, QColor("#006837"), "0.80–1.00 Vegetación muy vigorosa"),
                ]
                for lower, upper, color, label in classes:
                    symbol = QgsSymbol.defaultSymbol(layer.geometryType())
                    symbol.setColor(color)
                    try:
                        symbol.symbolLayer(0).setStrokeColor(QColor("#424242"))
                        symbol.symbolLayer(0).setStrokeWidth(0.2)
                    except Exception:
                        pass
                    ranges.append(QgsRendererRange(lower, upper, symbol, label))

                renderer = QgsGraduatedSymbolRenderer(field_name, ranges)
                renderer.setMode(QgsGraduatedSymbolRenderer.Custom)
                layer.setRenderer(renderer)

            QgsProject.instance().addMapLayer(layer)
            layer.triggerRepaint()
            self._log(f"Capa '{name}' cargada y simbolizada en QGIS.")

        except Exception as exc:
            self._log(f"No se pudo cargar/simbolizar la capa GeoJSON: {exc}")

    # --------------------------------------------------------
    # Gráfica científica
    # --------------------------------------------------------
    def _plot_series(self, series, csv_path, ctx=None):
        """
        Genera una gráfica científica coherente con los estadísticos solicitados.

        v0.4.2:
        - Si el usuario selecciona varios estadísticos, el gráfico YA NO muestra
          solo la primera casilla. Integra los estadísticos disponibles así:
            * Media: línea principal.
            * Mediana: línea secundaria.
            * Mínimo–Máximo: banda de rango espacial.
            * Desv. estándar: banda Media ± DE y/o eje secundario.
        - Si solo hay un estadístico disponible, grafica únicamente ese estadístico.
        - Señala fechas con baja cobertura relativa de píxeles válidos.
        """
        if not series:
            self._log("No se generó gráfico: la serie temporal AOI está vacía.")
            return

        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.dates as mdates
            import matplotlib.pyplot as plt
            import numpy as np
            import pandas as pd
            from matplotlib.lines import Line2D
            from matplotlib.patches import Patch

            ctx = ctx or {}
            requested_stats = ctx.get("stats", ["mean"]) or ["mean"]

            # ------------------------------------------------------------
            # 1) Construcción robusta de DataFrame desde JSON del backend
            # ------------------------------------------------------------
            rows = []
            for row in series:
                if not row.get("date"):
                    continue
                rec = {"date": row.get("date")}
                for stat in ("mean", "median", "min", "max", "stdDev"):
                    key = f"ndvi_{stat}"
                    if key in row and row.get(key) is not None:
                        try:
                            rec[key] = float(row.get(key))
                        except (TypeError, ValueError):
                            rec[key] = np.nan
                for key in ("n_valid_pixels", "n_images_input", "n_obs_same_date"):
                    if key in row and row.get(key) is not None:
                        try:
                            rec[key] = float(row.get(key))
                        except (TypeError, ValueError):
                            rec[key] = np.nan
                rows.append(rec)

            if not rows:
                self._log("No se generó gráfico: no hay fechas válidas en la serie.")
                return

            df = pd.DataFrame(rows)
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df = df.dropna(subset=["date"]).sort_values("date")

            # Agregación defensiva por fecha si llega más de un registro.
            numeric_cols = [c for c in df.columns if c != "date"]
            if df["date"].duplicated().any():
                agg = {}
                for col in numeric_cols:
                    if col == "n_valid_pixels":
                        agg[col] = "sum"
                    elif col in ("n_images_input", "n_obs_same_date"):
                        agg[col] = "sum"
                    else:
                        agg[col] = "mean"
                df = df.groupby("date", as_index=False).agg(agg)

            available = [
                stat for stat in requested_stats
                if f"ndvi_{stat}" in df.columns and df[f"ndvi_{stat}"].notna().any()
            ]

            if not available:
                # Compatibilidad defensiva con salidas antiguas.
                if "ndvi_mean" in df.columns and df["ndvi_mean"].notna().any():
                    available = ["mean"]
                else:
                    self._log("No se generó gráfico: no hay columnas NDVI numéricas.")
                    return

            # ------------------------------------------------------------
            # 2) Selección del estadístico principal
            # ------------------------------------------------------------
            if "mean" in available:
                main_stat = "mean"
            elif "median" in available:
                main_stat = "median"
            else:
                main_stat = available[0]

            main_key = f"ndvi_{main_stat}"
            dates = df["date"].to_list()
            main_vals = df[main_key].to_numpy(dtype=float)

            # ------------------------------------------------------------
            # 3) Diseño de figura
            # ------------------------------------------------------------
            fig, ax = plt.subplots(figsize=(12.5, 5.8))
            fig.patch.set_facecolor("white")
            ax.set_facecolor("white")

            has_ndvi_values = any(stat in available for stat in ("mean", "median", "min", "max"))

            if has_ndvi_values:
                y_candidates = []
                for stat in ("mean", "median", "min", "max"):
                    col = f"ndvi_{stat}"
                    if col in df.columns:
                        y_candidates.extend(df[col].dropna().astype(float).tolist())
                y_min = max(-0.15, min(0.15, min(y_candidates) - 0.08)) if y_candidates else 0.0
                y_max = min(1.0, max(0.90, max(y_candidates) + 0.06)) if y_candidates else 1.0

                bands = [
                    (-1.0, 0.20, "#F5F5F5", "<0.20 agua/suelo/sombra"),
                    (0.20, 0.40, "#FFE0B2", "0.20–0.40 baja/estresada"),
                    (0.40, 0.60, "#F0F4C3", "0.40–0.60 moderada"),
                    (0.60, 0.80, "#C8E6C9", "0.60–0.80 densa"),
                    (0.80, 1.00, "#A5D6A7", "0.80–1.00 muy vigorosa"),
                ]
                for lower, upper, color, _ in bands:
                    if upper >= y_min and lower <= y_max:
                        ax.axhspan(lower, upper, color=color, alpha=0.42, zorder=0)
            else:
                y_min = 0.0
                y_max = max(0.05, float(np.nanmax(main_vals)) * 1.25)

            # ------------------------------------------------------------
            # 4) Estadísticos integrados
            # ------------------------------------------------------------
            legend_handles = []

            # Banda Min–Max
            if "min" in available and "max" in available:
                ylo = df["ndvi_min"].to_numpy(dtype=float)
                yhi = df["ndvi_max"].to_numpy(dtype=float)
                ax.fill_between(
                    dates,
                    ylo,
                    yhi,
                    color="#81C784",
                    alpha=0.22,
                    linewidth=0,
                    zorder=1,
                    label="Rango espacial Mín–Máx",
                )
                legend_handles.append(
                    Patch(facecolor="#81C784", alpha=0.22, edgecolor="none", label="Rango espacial Mín–Máx")
                )
                # Límites del rango con línea fina para que el usuario vea Min y Max.
                ax.plot(dates, ylo, color="#D84315", lw=1.1, ls=":", alpha=0.80, zorder=2, label="Mínimo")
                ax.plot(dates, yhi, color="#1B5E20", lw=1.1, ls=":", alpha=0.80, zorder=2, label="Máximo")
                legend_handles.extend([
                    Line2D([0], [0], color="#D84315", lw=1.1, ls=":", label="Mínimo"),
                    Line2D([0], [0], color="#1B5E20", lw=1.1, ls=":", label="Máximo"),
                ])
            else:
                # Si solo está min o solo está max, se grafica como línea.
                if "min" in available:
                    ax.plot(dates, df["ndvi_min"], color="#D84315", lw=1.5, ls="--", marker="v",
                            ms=5.2, zorder=3, label="Mínimo")
                    legend_handles.append(Line2D([0], [0], color="#D84315", lw=1.5, ls="--", marker="v", label="Mínimo"))
                if "max" in available:
                    ax.plot(dates, df["ndvi_max"], color="#1B5E20", lw=1.5, ls="--", marker="^",
                            ms=5.2, zorder=3, label="Máximo")
                    legend_handles.append(Line2D([0], [0], color="#1B5E20", lw=1.5, ls="--", marker="^", label="Máximo"))

            # Banda Media ± DE, si existen ambas.
            if "mean" in available and "stdDev" in available:
                mean_vals = df["ndvi_mean"].to_numpy(dtype=float)
                sd_vals = df["ndvi_stdDev"].to_numpy(dtype=float)
                lower = np.clip(mean_vals - sd_vals, -1.0, 1.0)
                upper = np.clip(mean_vals + sd_vals, -1.0, 1.0)
                ax.fill_between(
                    dates,
                    lower,
                    upper,
                    color="#2E7D32",
                    alpha=0.14,
                    linewidth=0,
                    zorder=1.5,
                    label="Media ± DE espacial",
                )
                legend_handles.append(
                    Patch(facecolor="#2E7D32", alpha=0.14, edgecolor="none", label="Media ± DE espacial")
                )

            # Media
            if "mean" in available:
                ax.plot(
                    dates,
                    df["ndvi_mean"],
                    color="#2E7D32",
                    lw=2.3,
                    marker="o",
                    ms=6.8,
                    markeredgecolor="white",
                    markeredgewidth=0.9,
                    zorder=5,
                    label="Media",
                )
                legend_handles.append(
                    Line2D([0], [0], color="#2E7D32", lw=2.3, marker="o", label="Media")
                )

            # Mediana
            if "median" in available:
                ax.plot(
                    dates,
                    df["ndvi_median"],
                    color="#5D4037",
                    lw=1.9,
                    ls="--",
                    marker="s",
                    ms=5.5,
                    markeredgecolor="white",
                    markeredgewidth=0.8,
                    zorder=4,
                    label="Mediana",
                )
                legend_handles.append(
                    Line2D([0], [0], color="#5D4037", lw=1.9, ls="--", marker="s", label="Mediana")
                )

            # Si el usuario eligió solamente stdDev, se grafica en el eje principal.
            ax2 = None
            if available == ["stdDev"]:
                ax.plot(
                    dates,
                    df["ndvi_stdDev"],
                    color="#6A1B9A",
                    lw=2.1,
                    marker="D",
                    ms=5.7,
                    markeredgecolor="white",
                    markeredgewidth=0.8,
                    zorder=5,
                    label="Desv. estándar",
                )
                legend_handles.append(
                    Line2D([0], [0], color="#6A1B9A", lw=2.1, marker="D", label="Desv. estándar")
                )
                ax.set_ylabel("Desviación estándar del NDVI (AOI)", fontsize=11)
            elif "stdDev" in available and "mean" not in available:
                # Cuando hay DE con otros estadísticos pero sin media, se usa eje secundario.
                ax2 = ax.twinx()
                ax2.plot(
                    dates,
                    df["ndvi_stdDev"],
                    color="#6A1B9A",
                    lw=1.6,
                    ls="-.",
                    marker="D",
                    ms=4.8,
                    alpha=0.88,
                    zorder=3,
                    label="Desv. estándar",
                )
                ax2.set_ylabel("Desv. estándar NDVI", fontsize=10, color="#6A1B9A")
                ax2.tick_params(axis="y", labelcolor="#6A1B9A")
                legend_handles.append(
                    Line2D([0], [0], color="#6A1B9A", lw=1.6, ls="-.", marker="D", label="Desv. estándar")
                )
            else:
                ax.set_ylabel("NDVI (AOI)", fontsize=11)

            # ------------------------------------------------------------
            # 5) Tendencia del estadístico principal
            # ------------------------------------------------------------
            valid_main = np.isfinite(main_vals)
            if valid_main.sum() >= 4:
                vals = main_vals.copy()
                window = max(3, min(7, int(round(len(vals) / 5))))
                if window % 2 == 0:
                    window += 1
                half = window // 2
                trend = []
                for i in range(len(vals)):
                    lo = max(0, i - half)
                    hi = min(len(vals), i + half + 1)
                    trend.append(float(np.nanmean(vals[lo:hi])))
                ax.plot(
                    dates,
                    trend,
                    "--",
                    color="#1B5E20",
                    lw=1.6,
                    alpha=0.78,
                    zorder=3,
                    label=f"Tendencia suavizada ({STAT_LABELS.get(main_stat, main_stat)})",
                )
                legend_handles.append(
                    Line2D([0], [0], color="#1B5E20", lw=1.6, ls="--",
                           label=f"Tendencia suavizada ({STAT_LABELS.get(main_stat, main_stat)})")
                )

            # ------------------------------------------------------------
            # 6) Control de calidad por cobertura de píxeles válidos
            # ------------------------------------------------------------
            if "n_valid_pixels" in df.columns and df["n_valid_pixels"].notna().any():
                max_pix = float(df["n_valid_pixels"].max())
                if max_pix > 0:
                    low_mask = df["n_valid_pixels"] < (0.25 * max_pix)
                    if low_mask.any() and has_ndvi_values:
                        y_for_marks = df[main_key].to_numpy(dtype=float)
                        ax.scatter(
                            df.loc[low_mask, "date"],
                            y_for_marks[low_mask.to_numpy()],
                            marker="X",
                            s=92,
                            facecolor="#FF8F00",
                            edgecolor="black",
                            linewidth=0.6,
                            zorder=7,
                            label="Baja cobertura de píxeles válidos",
                        )
                        legend_handles.append(
                            Line2D([0], [0], marker="X", color="none", markerfacecolor="#FF8F00",
                                   markeredgecolor="black", markersize=8,
                                   label="Baja cobertura de píxeles válidos")
                        )

            # ------------------------------------------------------------
            # 7) Anotaciones del estadístico principal, no de todo el CSV
            # ------------------------------------------------------------
            if np.isfinite(main_vals).any():
                imax = int(np.nanargmax(main_vals))
                imin = int(np.nanargmin(main_vals))
                offset_max = (0, 16)
                offset_min = (0, -24)
                ax.annotate(
                    f"Máx. {STAT_LABELS.get(main_stat, main_stat)}",
                    (dates[imax], main_vals[imax]),
                    textcoords="offset points",
                    xytext=offset_max,
                    ha="center",
                    fontsize=8.7,
                    color="#1B5E20",
                    fontweight="bold",
                    arrowprops=dict(arrowstyle="-", color="#1B5E20", lw=0.8),
                )
                ax.annotate(
                    f"Mín. {STAT_LABELS.get(main_stat, main_stat)}",
                    (dates[imin], main_vals[imin]),
                    textcoords="offset points",
                    xytext=offset_min,
                    ha="center",
                    fontsize=8.7,
                    color="#BF360C",
                    fontweight="bold",
                    arrowprops=dict(arrowstyle="-", color="#BF360C", lw=0.8),
                )

            # ------------------------------------------------------------
            # 8) Títulos y leyenda
            # ------------------------------------------------------------
            comp = ctx.get("composite", "none")
            comp_label = {
                "none": "escenas agregadas por fecha",
                "monthly": "compuesto mensual",
                "yearly": "compuesto anual",
            }.get(comp, comp)

            stats_label = ", ".join(STAT_LABELS.get(stat, stat) for stat in available)
            subtitle = f"Estadísticos AOI visualizados: {stats_label} | {comp_label}"
            input_collection = ctx.get("input_collection", "sen2cor_sr")
            subtitle += f" | colección: {input_collection}"
            corr_mode = ctx.get("correction_mode", "none")
            if corr_mode and corr_mode != "none":
                subtitle += f" | ajuste radiométrico: {corr_mode} ({ctx.get('correction_years', '') or 'todos'})"
            if ctx.get("category"):
                subtitle += f" | filtro {ctx['field']}={ctx['category']}"

            ax.set_title(
                "Serie temporal del NDVI - Sen2Cor / Sentinel-2 (GEE)\n" + subtitle,
                fontsize=13,
                fontweight="bold",
                pad=10,
            )

            ax.set_xlabel("Fecha", fontsize=11)
            ax.set_ylim(y_min, y_max)
            ax.grid(alpha=0.24, lw=0.6)
            ax.spines[["top", "right"]].set_visible(False)

            locator = mdates.AutoDateLocator(minticks=5, maxticks=9)
            formatter = mdates.ConciseDateFormatter(locator)
            ax.xaxis.set_major_locator(locator)
            ax.xaxis.set_major_formatter(formatter)

            # Leyenda compacta, evitando tapar puntos mínimos.
            if legend_handles:
                ax.legend(
                    handles=legend_handles,
                    loc="upper center",
                    bbox_to_anchor=(0.5, -0.18),
                    ncol=3 if len(legend_handles) <= 6 else 4,
                    fontsize=8.2,
                    frameon=True,
                    framealpha=0.94,
                )

            fig.autofmt_xdate()
            fig.subplots_adjust(bottom=0.28)

            png_dir = os.path.dirname(csv_path) or tempfile.gettempdir()
            png_path = os.path.join(
                png_dir,
                os.path.basename(csv_path).replace(".csv", ".png"),
            )
            fig.savefig(png_path, dpi=220, bbox_inches="tight")
            plt.close(fig)

            self._log(f"Gráfico guardado: {png_path}")

        except Exception as exc:
            self._log(f"No se pudo graficar: {exc}")

# ============================================================
#  Controlador del plugin
# ============================================================

class NdviSen2CorPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None

    def initGui(self):
        icon_path = os.path.join(PLUGIN_DIR, "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
        self.action = QAction(icon, "NDVI - Sen2Cor", self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&NDVI - Sen2Cor", self.action)

    def unload(self):
        if self.action is not None:
            self.iface.removeToolBarIcon(self.action)
            self.iface.removePluginMenu("&NDVI - Sen2Cor", self.action)

    def run(self):
        dlg = NdviGeeDialog(self.iface, self.iface.mainWindow())
        dlg.show()
        dlg.exec_() if hasattr(dlg, "exec_") else dlg.exec()
