"""Desktop tracker window and dialogs. Run the tracker with python -m mx_eye."""

import base64
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

from . import config as cfg
from .cameras import CameraControls, CameraNameDiscovery
from .config import (
    PUPIL_METHODS,
    PupilMethod,
    PupilCoordinates,
    RecordingCodec,
    SourceMode,
    TrackingMode,
)
from .helptext import TIPS, add_tooltips
from .service import Service
from .widgets import EyeView, Parameter, Section, SeekSlider, label

ENUM_FIELDS = {
    ("recording", "codec"): RecordingCodec,
}


class Settings(W.QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = cfg.MxEyeConfigStore(config.value.model_copy(deep=True))
        self.fields = {}
        self.setWindowTitle("mx_eye · Settings")
        self.resize(500, 530)
        layout = W.QVBoxLayout(self)
        tabs = W.QTabWidget()
        layout.addWidget(tabs)
        specs = {
            "Camera": ("source", []),
            "Network": (
                "network",
                [
                    ("bind", "Tracker bind address", None),
                    ("data_port", "Sample port", 1024, 65535),
                    ("control_port", "Command port", 1024, 65535),
                ],
            ),
            "Recording": (
                "recording",
                [
                    ("directory", "Output folder", None),
                    ("buffer_mb", "Buffer size (MiB)", 8, 2048),
                    ("codec", "Codec", [item.value for item in RecordingCodec]),
                    ("camera_mjpeg_passthrough", "Use original camera MJPEG when available", True),
                ],
            ),
            "View": (
                "display",
                [
                    ("hz", "GUI refresh rate (Hz)", 1, 120),
                    ("pupil_mask", "Pupil mask", True),
                    ("cr_mask", "CR mask", True),
                    ("crosshairs", "Pupil / CR crosshairs", True),
                    ("template_circle", "Template size overlay", True),
                    ("template_inset", "Template / xcorr inset", True),
                ],
            ),
        }
        notes = {
            "Camera": "FPS and format are requests to the driver. The status bar shows measured acquisition rate. Recording is controlled separately by the Record button.",
            "Network": "For another computer, bind to 0.0.0.0 and use this tracker’s IP in the SDK. Samples use ZeroMQ PUB/SUB; commands use REQ/REP. Control is unauthenticated: use only your trusted local network.",
            "Recording": "MJPG: fast, lossy AVI. FFV1: lossless MKV, higher CPU demand. Full source frames are saved without overlays. Buffer overflow stops recording and marks the session incomplete; tracking continues.",
            "View": "Display refresh is independent of acquisition and tracking. Suspend displays to remove preview and plot work while tracking continues.",
        }
        for title, (group, rows) in specs.items():
            page = W.QWidget()
            form = W.QFormLayout(page)
            form.setVerticalSpacing(13)
            if title == "Camera":
                self.camera_controls = CameraControls(config.value.source, self)
                form.addRow(self.camera_controls)
            for key, text, *args in rows:
                value = getattr(getattr(config.value, group), key)
                if args[0] is True:
                    if value is None:
                        value = config.value.display.masks if key in ("pupil_mask", "cr_mask") else True
                    widget = W.QCheckBox()
                    widget.setChecked(value)
                elif isinstance(args[0], list):
                    widget = W.QComboBox()
                    widget.addItems(args[0])
                    widget.setCurrentText(str(value))
                elif args[0] is None:
                    widget = W.QLineEdit(str(value))
                else:
                    widget = (
                        W.QDoubleSpinBox() if key in ("fps", "speed", "hz") else W.QSpinBox()
                    )
                    widget.setRange(args[0], args[1])
                    widget.setValue(value)
                self.fields[group, key] = widget
                widget.setToolTip(TIPS.get(key, text))
                form.addRow(text, widget)
            if title == "Recording":
                choose = W.QPushButton("Choose output folder…")
                choose.clicked.connect(self.choose_folder)
                form.addRow("", choose)
            note = label(notes[title], "muted")
            note.setWordWrap(True)
            form.addRow(note)
            tabs.addTab(page, title)
            if parent is not None and parent.service.run and title != "View":
                page.setEnabled(False)
        if parent is not None and parent.service.run:
            tabs.setCurrentIndex(tabs.count() - 1)
        buttons = W.QDialogButtonBox(
            W.QDialogButtonBox.Save | W.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        add_tooltips(self)

    def choose_folder(self):
        path = W.QFileDialog.getExistingDirectory(self, "Recording folder")
        if path:
            self.fields["recording", "directory"].setText(path)

    def accept(self):
        if self.camera_controls.process is not None:
            W.QMessageBox.information(
                self,
                "Camera discovery",
                "Wait for camera discovery to finish before saving.",
            )
            return
        try:
            if self.parent() is None or not self.parent().service.run:
                self.config.value.source = self.camera_controls.values()
            for (group, key), widget in self.fields.items():
                if self.parent() is not None and self.parent().service.run and group != "display":
                    continue
                if isinstance(widget, W.QCheckBox):
                    value = widget.isChecked()
                elif isinstance(widget, W.QComboBox):
                    value = widget.currentText()
                    enum = ENUM_FIELDS.get((group, key))
                    if enum is not None:
                        value = enum(value)
                elif isinstance(widget, W.QLineEdit):
                    value = widget.text().strip()
                else:
                    value = widget.value()
                setattr(getattr(self.config.value, group), key, value)
        except ValueError as exc:
            W.QMessageBox.warning(self, "Check settings", str(exc))
            return
        super().accept()

    def done(self, result):
        self.camera_controls.stop_scan()
        super().done(result)


class Window(W.QMainWindow):
    def __init__(self):
        super().__init__()
        config = cfg.store()
        self.setWindowTitle("mx_eye · MXBI eye tracker")
        self.resize(1350, 870)
        self.setMinimumSize(980, 650)
        self.service = Service(config)
        self.pending = []
        self.parameters = {}
        self.last_payload = None
        self.navigation_id = 0
        self.navigation_pending = None
        self.deferred_video_action = None
        self.queued_seek = None
        self.history = deque(maxlen=1500)
        self.last_history_frame = None
        self.last_session = None
        self.rate_last = (time.monotonic(), 0, 0)
        self.rates = (0, 0)
        self._closing = False
        self.saved_source_path = config.value.source.path
        self.pending_video_path = None
        self._reason_text = self._error_text = ""
        self._reason_until = self._error_until = 0.0
        central = W.QWidget()
        self.setCentralWidget(central)
        layout = W.QVBoxLayout(central)
        layout.setContentsMargins(16, 12, 16, 10)
        toolbar = W.QHBoxLayout()
        toolbar.addWidget(label("mx_eye", "brand"))
        toolbar.addWidget(label("MXBI  /  PUPIL + CR", "muted"))
        toolbar.addStretch()
        toolbar.addWidget(label("Source:"))
        self.source = W.QComboBox()
        for caption, mode in [
            ("Camera", SourceMode.CAMERA),
            ("Video", SourceMode.VIDEO),
            ("Simulation", SourceMode.SIMULATION),
        ]:
            self.source.addItem(caption, mode.value)
        self.source.setCurrentIndex(
            max(0, self.source.findData(config.value.source.mode.value))
        )
        self.source.setFixedWidth(max(120, self.source.sizeHint().width()))
        toolbar.addWidget(self.source)
        self.open_button = W.QPushButton("Open video…")
        self.open_button.clicked.connect(self.open_video)
        toolbar.addWidget(self.open_button)
        self.start_button = W.QPushButton("Start")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self.start_or_stop)
        toolbar.addWidget(self.start_button)
        self.record_button = W.QPushButton("● Record")
        self.record_button.clicked.connect(self.toggle_recording)
        toolbar.addWidget(self.record_button)
        action_width = max(140, max(self.start_button.fontMetrics().horizontalAdvance(text)
                                   for text in ("Start", "Stop", "Open video…")) + 36)
        self.start_button.setFixedWidth(action_width)
        self.open_button.setFixedWidth(action_width)
        self.record_button.setFixedWidth(max(150, self.record_button.fontMetrics().horizontalAdvance("■ Stop recording") + 36))
        layout.addLayout(toolbar)
        files = W.QHBoxLayout()
        files.addStretch()
        self.display_pause = W.QPushButton("Suspend displays")
        self.display_pause.setCheckable(True)
        self.display_pause.setChecked(config.value.display.suspended)
        self.display_pause.setToolTip("Suspend video previews and plots while acquisition and tracking continue.")
        self.display_pause.toggled.connect(self.display_changed)
        files.addWidget(self.display_pause)
        layout.addLayout(files)
        self.camera = W.QSpinBox(self)
        self.camera.setRange(0, 99)
        self.camera.setValue(config.value.source.camera)
        self.camera.hide()
        source_row = W.QHBoxLayout()
        self.source_label = label("", "muted")
        self.source_label.setTextInteractionFlags(C.Qt.TextSelectableByMouse)
        self.source_label.setSizePolicy(W.QSizePolicy.Ignored, W.QSizePolicy.Preferred)
        source_row.addWidget(self.source_label, 1)
        self.requested_fps = label("", "muted")
        source_row.addWidget(self.requested_fps)
        layout.addLayout(source_row)
        self.file_menu = self.menuBar().addMenu("File")
        for title, callback in [
            ("Open video…", self.open_video),
            ("Save configuration…", self.save_config),
            ("Load configuration…", self.load_config),
            ("Save template…", self.save_template),
            ("Load template…", self.load_template),
            ("Exit", self.close),
        ]:
            self.file_menu.addAction(title, callback)
        self.menuBar().addAction("Settings", self.settings)
        calibration = self.menuBar().addMenu("Calibration")
        placeholder = calibration.addAction("Calibration tools · planned")
        placeholder.setEnabled(False)
        tools = self.menuBar().addMenu("Tools")
        tools.addAction("Timing / performance diagnostics…", self.show_diagnostics)
        self.diagnostics = None
        self.diagnostic_values = {}
        self.source.currentIndexChanged.connect(self.source_controls_changed)
        self.source_controls_changed()
        metrics_bar = W.QFrame()
        metrics_bar.setObjectName("metricsBar")
        metrics_bar.setStyleSheet(
            "QFrame#metricsBar { background:#192331; border-radius:6px; }"
        )
        metrics_row = W.QHBoxLayout(metrics_bar)
        metrics_row.setContentsMargins(13, 8, 13, 8)
        metrics_row.setSpacing(12)
        self.metrics = label("ACQ —  TRACK —  PROC —  SKIPPED —  VIDEO —", "muted")
        self.metrics.setFont(G.QFontDatabase.systemFont(G.QFontDatabase.FixedFont))
        self.metrics.setFixedWidth(self.metrics.fontMetrics().horizontalAdvance(
            "ACQ 999.9 fps  TRACK 999.9 fps  PROC 999.99 ms  SKIPPED 999999  VIDEO 999999 frames · buffer 9999"
        ))
        self.metrics.setToolTip("Acquisition, tracking and recording status")
        metrics_row.addWidget(self.metrics)
        self.reason_label = label("", "metricAlert")
        metrics_row.addWidget(self.reason_label, 1)
        layout.addWidget(metrics_bar)
        split = W.QSplitter(C.Qt.Horizontal)
        layout.addWidget(split, 1)
        main = W.QWidget()
        main_layout = W.QVBoxLayout(main)
        main_layout.setContentsMargins(0, 0, 6, 0)
        self.coordinates_row = W.QWidget()
        output_row = W.QHBoxLayout(self.coordinates_row)
        output_row.setContentsMargins(0, 0, 0, 0)
        output_row.addWidget(label("Pupil-only output", "muted"))
        self.coordinates = W.QComboBox()
        self.coordinates.addItem(
            "Absolute · full image", PupilCoordinates.ABSOLUTE.value
        )
        self.coordinates.addItem(
            "Relative · yellow ROI", PupilCoordinates.RELATIVE.value
        )
        self.coordinates.setCurrentIndex(
            max(
                0,
                self.coordinates.findData(
                    config.value.tracking.pupil_coordinates.value
                ),
            )
        )
        self.coordinates.setToolTip(
            "Pupil-only X/Y output: absolute uses the full-image top-left; relative uses the yellow ROI top-left. Applies to plots, saved samples and SDK. A moving ROI changes the relative origin."
        )
        self.coordinates.currentIndexChanged.connect(self.schedule_parameters)
        output_row.addWidget(self.coordinates)
        output_row.addStretch()
        self.output_signature = None
        vertical = W.QSplitter(C.Qt.Vertical)
        main_layout.addWidget(vertical, 1)
        views = W.QSplitter(C.Qt.Horizontal)
        self.full = EyeView()
        self.eye = EyeView(crop=True)

        def toggle(text, key, default=True, color=None):
            box = W.QCheckBox(text)
            value = getattr(config.value.display, key)
            box.setChecked(default if value is None else value)
            if color:
                box.setStyleSheet("color:" + color + ";")
            return box

        self.pupil_mask = toggle(
            "Pupil", "pupil_mask", config.value.display.masks, "#67b5ff"
        )
        self.cr_mask = toggle("CR", "cr_mask", config.value.display.masks, "#ff8585")
        self.crosshairs = toggle("Centers", "crosshairs")
        self.circle = toggle("Template size", "template_circle", True, "#d777df")
        self.inset = toggle("Template inset", "template_inset")
        for view, title, hint in [
            (self.eye, "EYE DETAIL", "Left click = PUPIL · Right click = CR"),
            (
                self.full,
                "SOURCE",
                "Right click = TEMPLATE · Drag ROI · Shift-drag = new ROI",
            ),
        ]:
            panel = W.QWidget()
            column = W.QVBoxLayout(panel)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(3)
            header = W.QHBoxLayout()
            header.addWidget(label(title, "muted"))
            header.addStretch()
            for box in (
                (self.pupil_mask, self.cr_mask, self.crosshairs)
                if view is self.eye
                else (self.circle, self.inset)
            ):
                box.setParent(self)
                box.hide()
            column.addLayout(header)
            column.addWidget(view, 1)
            instructions = label(hint, "muted")
            instructions.setWordWrap(True)
            column.addWidget(instructions)
            views.addWidget(panel)
            view.action.connect(self.view_action)
        views.setSizes([460, 460])
        vertical.addWidget(views)
        plot_panel = W.QWidget()
        plot_layout = W.QVBoxLayout(plot_panel)
        plot_layout.setContentsMargins(0, 0, 0, 0)
        plot_layout.addWidget(self.coordinates_row)
        self.coordinates_row.setVisible(
            config.value.tracking.tracking_mode is TrackingMode.PUPIL_ONLY
        )
        plots = W.QSplitter(C.Qt.Horizontal)
        pg.setConfigOptions(antialias=False, background="#10151d", foreground="#93a5bd")
        self.trace = pg.PlotWidget(title="Eye signal · pixels")
        self.trace.addLegend(offset=(8, 8))
        self.trace.setLabel("bottom", "Time", "s")
        self.trace.showGrid(x=True, y=True, alpha=0.15)
        self.tx = self.trace.plot(pen=pg.mkPen("#6bc9f2", width=1.4), name="x")
        self.ty = self.trace.plot(pen=pg.mkPen("#efa966", width=1.4), name="y")
        self.xy = pg.PlotWidget(title="x / y · pixels")
        self.xy.setAspectLocked(True)
        self.xy.invertY(True)
        self.xyline = self.xy.plot(pen=pg.mkPen("#51728f", width=1))
        self.xypoint = self.xy.plot(
            pen=None, symbol="o", symbolSize=7, symbolBrush="#6bc9f2"
        )
        plots.addWidget(self.trace)
        plots.addWidget(self.xy)
        plots.setSizes([460, 460])
        plot_layout.addWidget(plots, 1)
        vertical.addWidget(plot_panel)
        vertical.setSizes([500, 190])
        playback = W.QHBoxLayout()
        self.pause = W.QPushButton("Play/Pause")
        self.pause.setCheckable(True)
        self.pause.toggled.connect(self.pause_changed)
        self.step = W.QPushButton("+1 frame")
        self.step.clicked.connect(lambda: self.step_video(1))
        self.back = W.QPushButton("−1 frame")
        self.back.clicked.connect(lambda: self.step_video(-1))
        self.timeline = SeekSlider()
        self.timeline.seek.connect(self.seek_video)
        playback.addWidget(self.pause)
        playback.addWidget(self.back)
        playback.addWidget(self.step)
        playback.addWidget(label("Speed", "muted"))
        self.speed = W.QDoubleSpinBox()
        self.speed.setRange(0.05, 8)
        self.speed.setSingleStep(0.25)
        self.speed.setSuffix("×")
        self.speed.setValue(config.value.source.speed)
        self.speed.setKeyboardTracking(False)
        self.speed.valueChanged.connect(lambda value: self.call("speed", speed=value))
        playback.addWidget(self.speed)
        playback.addWidget(self.timeline, 1)
        self.frame_label = label("Frame —", "muted")
        playback.addWidget(self.frame_label)
        main_layout.addLayout(playback)
        split.addWidget(main)
        sidebar = W.QScrollArea()
        sidebar.setWidgetResizable(True)
        sidebar.setMinimumWidth(260)
        sidebar.setMaximumWidth(400)
        controls = W.QWidget()
        body = W.QVBoxLayout(controls)
        body.setContentsMargins(5, 0, 5, 0)
        sidebar.setWidget(controls)
        self.mode = W.QComboBox()
        self.mode.addItems([mode.value for mode in TrackingMode])
        self.mode.setCurrentText(config.value.tracking.tracking_mode.value)
        self.mode.currentTextChanged.connect(self.schedule_parameters)
        self.mode.currentTextChanged.connect(
            lambda mode: self.coordinates_row.setVisible(
                TrackingMode(mode) is TrackingMode.PUPIL_ONLY
            )
        )
        mode_row = W.QHBoxLayout()
        mode_row.addWidget(label("Tracking mode", "muted"))
        mode_row.addWidget(self.mode, 1)
        body.addLayout(mode_row)
        method_row = W.QHBoxLayout()
        method_row.addWidget(label("Pupil method", "muted"))
        self.pupil_method = W.QComboBox()
        for method, title in PUPIL_METHODS.items():
            self.pupil_method.addItem(title, method.value)
        self.pupil_method.setCurrentIndex(max(0, self.pupil_method.findData(config.value.tracking.pupil_method.value)))
        self.pupil_method.currentIndexChanged.connect(self.update_method_controls)
        self.pupil_method.currentIndexChanged.connect(self.schedule_parameters)
        method_row.addWidget(self.pupil_method, 1)
        body.addLayout(method_row)
        self.track_label = label("Waiting for source", "muted")
        self.track_label.setWordWrap(True)
        body.addWidget(self.track_label)
        groups = [
            (
                "Pupil",
                True,
                [
                    ("pupil_thr", "Dark threshold", 0, 255, 1),
                    ("pupil_min", "Minimum area", 1, 3000, 1),
                    ("pupil_max", "Maximum area", 10, 15000, 10),
                ],
            ),
            (
                "Pupil edge methods", False,
                [("pupil_rays", "Radial rays", 16, 128, 1),
                 ("pupil_edge_contrast", "Edge contrast", 1, 100, 1),
                 ("pupil_edge_threshold", "Canny threshold", 1, 255, 1),
                 ("pupil_fit_error", "Ellipse tolerance", 0.5, 10, 0.1)],
            ),
            (
                "Adaptive pupil", False,
                [("pupil_adaptive_window", "Window", 3, 301, 2),
                 ("pupil_adaptive_offset", "Offset", 0, 50, 1)],
            ),
            (
                "Corneal reflection",
                True,
                [
                    ("cr_thr", "Bright threshold", 0, 255, 1),
                    ("cr_min", "Minimum area", 1, 300, 1),
                    ("cr_max", "Maximum area", 1, 2000, 1),
                ],
            ),
            (
                "Tracking stability",
                False,
                [
                    ("pupil_gate", "Pupil gate", 1, 100, 1),
                    ("cr_gate", "CR gate", 1, 100, 1),
                    ("max_pair_dist", "Pupil–CR distance", 1, 150, 1),
                    ("max_pair_vec_change", "Vector change", 1, 100, 1),
                    ("reacquire_after_frames", "Reacquire frames", 1, 60, 1),
                ],
            ),
            (
                "Template / ROI",
                False,
                [
                    ("template_radius", "Template radius", 6, 150, 1),
                    ("template_search_size", "Search size", 100, 1600, 10),
                    ("template_min_corr", "Min. correlation", 0, 1, 0.01),
                ],
            ),
        ]
        for title, opened, rows in groups:
            section = Section(title, opened)
            body.addWidget(section)
            if title == "Pupil edge methods":
                self.edge_section = section
            elif title == "Adaptive pupil":
                self.adaptive_section = section
            for key, text, lo, hi, step in rows:
                param = Parameter(
                    text, getattr(config.value.tracking, key), lo, hi, step
                )
                for widget in (param, param.spin, param.slider):
                    widget.setToolTip(TIPS[key])
                for caption in param.findChildren(W.QLabel):
                    caption.setToolTip(TIPS[key])
                self.parameters[key] = param
                param.changed.connect(self.schedule_parameters)
                if key in ("pupil_min", "pupil_max", "cr_min", "cr_max"):
                    param.changed.connect(
                        lambda key=key: self.eye.show_area_limit(
                            key, self.parameters[key].spin.value()
                        )
                    )
                section.body.addWidget(param)
            if title == "Template / ROI":
                self.template_on = W.QCheckBox("Follow template")
                self.template_on.setChecked(config.value.tracking.template_tracking)
                self.template_on.toggled.connect(self.schedule_parameters)
                section.body.addWidget(self.template_on)
                clear = W.QPushButton("Clear template")
                clear.clicked.connect(lambda: self.call("clear_template"))
                section.body.addWidget(clear)
                self.template_label = label(
                    "Right click the full view to capture a template.", "muted"
                )
                self.template_label.setWordWrap(True)
                section.body.addWidget(self.template_label)
        reset = W.QPushButton("Reset tracking history")
        reset.clicked.connect(lambda: self.call("reset"))
        body.addWidget(reset)
        body.addStretch()
        split.addWidget(sidebar)
        split.setSizes([1020, 290])
        self.status = label("Ready", "muted")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(C.Qt.TextSelectableByMouse)
        layout.addWidget(self.status)
        self.param_timer = C.QTimer(self)
        self.param_timer.setSingleShot(True)
        self.param_timer.timeout.connect(self.send_parameters)
        self.timer = C.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(max(8, round(1000 / config.value.display.hz)))
        self.update_method_controls()
        self.display_changed(self.display_pause.isChecked())
        add_tooltips(self)
        self.camera_names = {}
        self.camera_name_discovery = CameraNameDiscovery(self)
        self.camera_name_discovery.finished.connect(self.camera_names_received)
        C.QTimer.singleShot(0, self.camera_name_discovery.start)
        W.QApplication.instance().installEventFilter(self)

    def camera_names_received(self, names):
        self.camera_names = names
        selected = self.camera.value()
        if selected in names and not self.service.run:
            self.service.config.value.source.camera_name = names[selected]

    def source_mode(self):
        return SourceMode(self.source.currentData())

    def tracking_mode(self):
        return TrackingMode(self.mode.currentText())

    def pupil_coordinates(self):
        return PupilCoordinates(self.coordinates.currentData())

    def call(self, command, callback=None, **args):
        self.pending.append((self.service.submit(command, **args), callback))

    def update_method_controls(self, *args):
        method = PupilMethod(self.pupil_method.currentData())
        self.edge_section.setVisible(method in (PupilMethod.STARBURST, PupilMethod.EDGE_ELLIPSE))
        self.adaptive_section.setVisible(method is PupilMethod.ADAPTIVE)
        for key in ("pupil_rays", "pupil_edge_contrast", "pupil_edge_threshold", "pupil_fit_error",
                    "pupil_adaptive_window", "pupil_adaptive_offset"):
            relevant = ((key == "pupil_rays" and method is PupilMethod.STARBURST)
                        or (key in ("pupil_edge_contrast", "pupil_fit_error")
                            and method in (PupilMethod.STARBURST, PupilMethod.EDGE_ELLIPSE))
                        or (key == "pupil_edge_threshold" and method is PupilMethod.EDGE_ELLIPSE)
                        or (key.startswith("pupil_adaptive") and method is PupilMethod.ADAPTIVE))
            self.parameters[key].setEnabled(relevant)
        self.parameters["pupil_thr"].setEnabled(method is PupilMethod.THRESHOLD)

    def update_alert(self, *args):
        now = time.monotonic()
        error = self._error_text if now < self._error_until else ""
        reason = self._reason_text if now < self._reason_until else ""
        message = error or (f"No valid position: {reason}" if reason else "")
        self.reason_label.setToolTip(message)
        self.reason_label.setText(self.reason_label.fontMetrics().elidedText(
            message, C.Qt.ElideRight, max(0, self.reason_label.width() - 8)))
        self.reason_label.setStyleSheet("color:#ff817f;" if error else "color:#f0ad74;")

    def display_changed(self, suspended):
        self.display_pause.setText("Resume displays" if suspended else "Suspend displays")
        self.timer.setInterval(200 if suspended else max(8, round(1000 / self.service.config.value.display.hz)))
        self.full.setEnabled(not suspended)
        self.eye.setEnabled(not suspended)
        if self.service.run:
            self.call("display", suspended=suspended)
        else:
            self.service.config.value.display.suspended = suspended

    def schedule_parameters(self, *args):
        if hasattr(self, "param_timer"):
            self.param_timer.start(80)

    def send_parameters(self):
        tracking = self.service.config.value.tracking.model_copy(deep=True)
        for key, param in self.parameters.items():
            setattr(tracking, key, param.spin.value())
        tracking.tracking_mode = self.tracking_mode()
        tracking.pupil_method = PupilMethod(self.pupil_method.currentData())
        tracking.pupil_coordinates = self.pupil_coordinates()
        tracking.template_tracking = self.template_on.isChecked()
        self.call("config", tracking=tracking)

    def open_video(self):
        path, _ = W.QFileDialog.getOpenFileName(
            self,
            "Open eye video",
            "",
            "Video (*.mp4 *.avi *.mkv *.mov *.m4v);;All files (*)",
        )
        if path:
            self.saved_source_path = path
            self.source.setCurrentIndex(
                max(0, self.source.findData(SourceMode.VIDEO.value))
            )
            self.source_label.setText(path)
            self.full.reset_zoom()
            self.pending_video_path = path
            if self.service.run and self.service.snapshot().state != "stopping":
                self.call("stop")

    def start(self):
        self.send_parameters()
        config = cfg.MxEyeConfigStore(self.service.config.value.model_copy(deep=True))
        config.value.source.mode = self.source_mode()
        config.value.source.path = self.saved_source_path
        config.value.source.camera = self.camera.value()
        config.value.source.camera_name = self.camera_names.get(self.camera.value(), config.value.source.camera_name)
        config.value.source.speed = self.speed.value()
        for key, param in self.parameters.items():
            setattr(config.value.tracking, key, param.spin.value())
        config.value.tracking.tracking_mode = self.tracking_mode()
        config.value.tracking.pupil_method = PupilMethod(self.pupil_method.currentData())
        config.value.tracking.pupil_coordinates = self.pupil_coordinates()
        config.value.tracking.template_tracking = self.template_on.isChecked()
        self.call("settings", config=config, callback=lambda: self.call("start"))

    def source_controls_changed(self, *args):
        video = self.source_mode() is SourceMode.VIDEO
        self.open_button.setVisible(video)
        self.start_button.setVisible(not video)
        self.record_button.setEnabled(False)
        if (self.service.run and self.service.config.value.source.mode is not self.source_mode()
                and self.service.snapshot().state != "stopping"):
            self.call("stop")

    def start_or_stop(self):
        if self.service.run:
            self.call("stop")
        else:
            self.start()

    def toggle_recording(self):
        enabled = not self.service.run["recording"].is_set()
        self.call("record", enabled=enabled)

    def show_diagnostics(self):
        if self.diagnostics is None:
            self.diagnostics = W.QDialog(self)
            self.diagnostics.setWindowTitle("Timing / performance diagnostics")
            form = W.QFormLayout(self.diagnostics)
            for title in ("Acquisition rate", "Tracking rate", "Tracking processing",
                          "GUI refresh rate", "GUI callback", "Acquired frames skipped by tracking",
                          "Send errors", "Recording buffer"):
                value = label("—")
                form.addRow(title, value)
                self.diagnostic_values[title] = value
            note = label("Uses existing counters and the latest tracking-loop duration. GUI callback timing is measured only while this window is open. Detailed tracking-stage profiling will be added separately.", "muted")
            note.setWordWrap(True)
            form.addRow(note)
        self.diagnostics.show()
        self.diagnostics.raise_()

    def apply_view_settings(self, display):
        self.service.config.value.display = display.model_copy(deep=True)
        for box, key in ((self.pupil_mask, "pupil_mask"), (self.cr_mask, "cr_mask"),
                         (self.crosshairs, "crosshairs"), (self.circle, "template_circle"),
                         (self.inset, "template_inset")):
            value = getattr(display, key)
            box.setChecked(display.masks if value is None and "mask" in key else True if value is None else value)
        self.display_changed(display.suspended)
        if self.service.run:
            self.call("display", suspended=display.suspended, hz=display.hz)

    def settings(self):
        config = cfg.MxEyeConfigStore(self.service.config.value.model_copy(deep=True))
        config.value.source.camera = self.camera.value()
        dialog = Settings(config, self)
        if dialog.exec() == W.QDialog.Accepted:
            if not self.service.run:
                self.camera.setValue(dialog.config.value.source.camera)
                self.call("settings", config=dialog.config)
            self.apply_view_settings(dialog.config.value.display)

    def pause_changed(self, paused):
        self.pause.setText("Play" if paused else "Pause")
        if not paused and self.timeline.value() >= self.timeline.maximum():
            self.seek_video(0)
            self.deferred_video_action = ("pause", {"paused": False})
            return
        self.call("pause", paused=paused)

    def step_video(self, direction):
        if (
            not self.step.isEnabled()
            or self.navigation_pending is not None
            or self.last_payload is None
        ):
            return
        self.navigate_video(
            "step", direction=direction, from_frame=self.last_payload["source_index"]
        )

    def seek_video(self, frame):
        if not self.timeline.isEnabled():
            return
        if self.navigation_pending is not None:
            self.queued_seek = frame
            return
        self.navigate_video("seek", frame=frame)

    def navigate_video(self, command, **args):
        self.navigation_id += 1
        self.navigation_pending = self.navigation_id
        with C.QSignalBlocker(self.pause):
            self.pause.setChecked(True)
        self.pause.setText("Play")
        self.full.setEnabled(False)
        self.eye.setEnabled(False)
        self.call(command, navigation_id=self.navigation_id, **args)

    def view_action(self, command, args):
        if self.navigation_pending is not None:
            return
        if (
            self.service.config.value.source.mode is SourceMode.VIDEO
            and self.last_payload is not None
        ):
            # Freeze precisely the displayed frame before acting on its pixels.
            self.navigate_video("seek", frame=self.last_payload["source_index"])
            self.deferred_video_action = (command, args)
        else:
            self.call(command, **args)

    def eventFilter(self, watched, event):
        if event.type() == C.QEvent.KeyPress and event.key() in (
            C.Qt.Key_Left,
            C.Qt.Key_Right,
        ):
            focus = W.QApplication.focusWidget()
            editing = isinstance(
                focus, (W.QLineEdit, W.QAbstractSpinBox, W.QComboBox)
            ) or (isinstance(focus, W.QSlider) and focus is not self.timeline)
            if self.isActiveWindow() and self.step.isEnabled() and not editing:
                self.step_video(-1 if event.key() == C.Qt.Key_Left else 1)
                return True
        return super().eventFilter(watched, event)

    def save_config(self):
        path, _ = W.QFileDialog.getSaveFileName(
            self, "Save configuration", "mx_eye_config.json", "JSON (*.json)"
        )
        if path:
            config = cfg.MxEyeConfigStore(
                self.service.config.value.model_copy(deep=True)
            )
            config.value.display.pupil_mask = self.pupil_mask.isChecked()
            config.value.display.cr_mask = self.cr_mask.isChecked()
            config.value.display.crosshairs = self.crosshairs.isChecked()
            config.value.display.template_circle = self.circle.isChecked()
            config.value.display.template_inset = self.inset.isChecked()
            config.value.display.suspended = self.display_pause.isChecked()
            config.value.source.mode = self.source_mode()
            config.value.source.path = self.saved_source_path
            config.value.source.camera = self.camera.value()
            config.value.source.speed = self.speed.value()
            for key, param in self.parameters.items():
                setattr(config.value.tracking, key, param.spin.value())
            config.value.tracking.tracking_mode = self.tracking_mode()
            config.value.tracking.pupil_method = PupilMethod(self.pupil_method.currentData())
            config.value.tracking.pupil_coordinates = self.pupil_coordinates()
            config.value.tracking.template_tracking = self.template_on.isChecked()
            try:
                config.save(Path(path))
            except (ValueError, OSError) as exc:
                W.QMessageBox.warning(self, "Cannot save", str(exc))

    def save_template(self):
        template = self.service.config.value.template
        if not template:
            W.QMessageBox.information(
                self,
                "Template",
                "Right click the source video to capture a template first.",
            )
            return
        path, _ = W.QFileDialog.getSaveFileName(
            self, "Save template", "template.png", "PNG (*.png)"
        )
        if path:
            try:
                Path(path).write_bytes(base64.b64decode(template.png))
            except (ValueError, OSError) as exc:
                W.QMessageBox.warning(self, "Cannot save template", str(exc))

    def load_template(self):
        path, _ = W.QFileDialog.getOpenFileName(
            self, "Load template", "", "Images (*.png *.tif *.tiff *.bmp *.jpg *.jpeg)"
        )
        if not path:
            return
        try:
            patch = cv2.imdecode(
                np.frombuffer(Path(path).read_bytes(), np.uint8), cv2.IMREAD_GRAYSCALE
            )
            if patch is None or not patch.size:
                raise ValueError("Cannot read this template image.")
            ok, encoded = cv2.imencode(".png", patch)
            if not ok:
                raise ValueError("Cannot encode template.")
            r = (self.last_payload or {}).get("result", {})
            x, y, w, h = r.get("roi", self.service.config.value.tracking.roi)
            existing = self.service.config.value.template
            center = (
                r.get("template_center")
                or (existing.center if existing else None)
                or [x + w / 2, y + h / 2]
            )
            anchor = (
                r.get("template_anchor")
                or (existing.anchor if existing else None)
                or center
            )
            self.parameters["template_radius"].set_value(
                max(6, min(150, (min(patch.shape) - 1) // 2))
            )
            self.template_on.setChecked(True)
            self.send_parameters()
            template = cfg.TemplateConfig(
                png=base64.b64encode(encoded).decode("ascii"),
                anchor=anchor,
                center=center,
            )
            self.call("load_template", template=template)
        except (ValueError, OSError, cv2.error) as exc:
            W.QMessageBox.warning(self, "Cannot load template", str(exc))

    def load_config(self):
        if self.service.run:
            W.QMessageBox.information(
                self, "Stop first", "Stop the session before loading a configuration."
            )
            return
        path, _ = W.QFileDialog.getOpenFileName(
            self, "Load configuration", "", "JSON (*.json)"
        )
        if path:
            try:
                config = cfg.MxEyeConfigStore.load(Path(path))
            except (OSError, ValueError, KeyError) as exc:
                W.QMessageBox.warning(self, "Cannot load", str(exc))
                return
            self.call("settings", config=config)
            self.source.setCurrentIndex(
                max(0, self.source.findData(config.value.source.mode.value))
            )
            self.saved_source_path = config.value.source.path
            self.source_label.setText(self.saved_source_path)
            self.mode.setCurrentText(config.value.tracking.tracking_mode.value)
            self.pupil_method.setCurrentIndex(max(0, self.pupil_method.findData(config.value.tracking.pupil_method.value)))
            self.display_pause.setChecked(config.value.display.suspended)
            self.apply_view_settings(config.value.display)
            self.coordinates.setCurrentIndex(
                max(
                    0,
                    self.coordinates.findData(
                        config.value.tracking.pupil_coordinates.value
                    ),
                )
            )
            for key, param in self.parameters.items():
                param.set_value(getattr(config.value.tracking, key))
            self.template_on.setChecked(config.value.tracking.template_tracking)
            self.camera.setValue(config.value.source.camera)
            self.requested_fps.setText(f"Requested {config.value.source.fps:g} fps")
            with C.QSignalBlocker(self.speed):
                self.speed.setValue(config.value.source.speed)
            for box, key, default in [
                (self.pupil_mask, "pupil_mask", config.value.display.masks),
                (self.cr_mask, "cr_mask", config.value.display.masks),
                (self.crosshairs, "crosshairs", True),
                (self.circle, "template_circle", True),
                (self.inset, "template_inset", True),
            ]:
                value = getattr(config.value.display, key)
                box.setChecked(default if value is None else value)

    def refresh(self):
        measuring = self.diagnostics is not None and self.diagnostics.isVisible()
        if measuring:
            started = time.perf_counter()
            previous = getattr(self, "diagnostic_previous", None)
            interval = started - previous if previous is not None else None
            self.diagnostic_previous = started
        else:
            self.diagnostic_previous = None
        self.refresh_contents()
        if measuring:
            state = self.service.snapshot()
            values = (f"{self.rates[0]:.1f} fps", f"{self.rates[1]:.1f} fps",
                      f"{state.stats.processing_us / 1000:.3f} ms (latest sample)",
                      f"{1 / interval:.1f} Hz measured" if interval else "—",
                      f"{(time.perf_counter() - started) * 1000:.3f} ms",
                      str(state.stats.tracking_skips), str(state.stats.send_errors),
                      str(int(state.stats.enqueued - state.stats.written)))
            for widget, value in zip(self.diagnostic_values.values(), values):
                widget.setText(value)

    def refresh_contents(self):
        for future, callback in list(self.pending):
            if future.done():
                self.pending.remove((future, callback))
                try:
                    future.result()
                    if callback:
                        callback()
                except Exception as exc:
                    self.navigation_pending = None
                    self.deferred_video_action = None
                    self.queued_seek = None
                    self.full.setEnabled(not self.display_pause.isChecked())
                    self.eye.setEnabled(not self.display_pause.isChecked())
                    W.QMessageBox.warning(
                        self,
                        "mx_eye",
                        str(exc)
                        or f"{type(exc).__name__}: the operation could not complete.",
                    )
        state = self.service.snapshot()
        mode = self.source_mode()
        if self.pending_video_path and not self.service.run and not self.pending:
            if (
                mode is SourceMode.VIDEO
                and self.saved_source_path == self.pending_video_path
            ):
                self.pending_video_path = None
                self.start()
            else:
                self.pending_video_path = None
        source = self.service.config.value.source
        name = (Path(self.saved_source_path).name if mode is SourceMode.VIDEO
                else "Simulation" if mode is SourceMode.SIMULATION
                else self.camera_names.get(self.camera.value()) or source.camera_name or source.device or f"Camera {self.camera.value()}")
        self.source_label.setText(self.source_label.fontMetrics().elidedText(
            name, C.Qt.ElideMiddle, max(0, self.source_label.width())))
        self.source_label.setToolTip(self.saved_source_path if mode is SourceMode.VIDEO else name)
        fps = state.source.fps if mode is SourceMode.VIDEO else source.fps
        self.requested_fps.setText(f"{fps:g} fps" + (" requested" if mode is SourceMode.CAMERA else ""))
        active = state.state in ("running", "starting", "stopping")
        self.start_button.setText("Stop" if active else "Start")
        self.start_button.setEnabled(not self.pending and state.state != "stopping")
        self.source.setEnabled(state.state != "stopping" and not self.pending)
        self.open_button.setEnabled(not self.pending and not self._closing)
        run = self.service.run
        recording = bool(run and run["recording"].is_set())
        draining = bool(run and not recording and not run["writer_done"].is_set())
        self.record_button.setText("■ Stop recording" if recording else "Finalizing…" if draining else "● Record")
        self.record_button.setEnabled(state.state == "running" and mode is not SourceMode.VIDEO
                                      and not draining and not self.pending and not state.stats.record_fault)
        video = active and self.service.config.value.source.mode is SourceMode.VIDEO
        if not video:
            self.navigation_pending = None
            self.queued_seek = None
            self.deferred_video_action = None
            self.full.setEnabled(not self.display_pause.isChecked())
            self.eye.setEnabled(not self.display_pause.isChecked())
        self.coordinates.setEnabled(self.tracking_mode() is TrackingMode.PUPIL_ONLY)
        self.speed.setEnabled(mode is SourceMode.VIDEO and state.state != "stopping")
        for widget in (self.pause, self.back, self.step, self.timeline):
            widget.setEnabled(video)
        self.pause.setEnabled(video and self.navigation_pending is None)
        if video and not self.pending and self.navigation_pending is None:
            with C.QSignalBlocker(self.pause):
                self.pause.setChecked(state.paused)
            self.pause.setText("Play" if state.paused else "Pause")
        if not video and self.pause.isChecked():
            with C.QSignalBlocker(self.pause):
                self.pause.setChecked(False)
            self.pause.setText("Pause")
        if not video:
            self.pause.setText("Play/Pause")
        self.status.setText(
            f"{state.state.upper()}  ·  {state.message}"
            + (f"   {state.directory}" if state.directory else "")
        )
        if video and self.navigation_pending is not None:
            self.status.setText("Seeking video frame…")
        stats = state.stats
        if state.session != self.last_session:
            self.history.clear()
            self.last_payload = None
            self.last_history_frame = None
            self.last_session = state.session
            self.rate_last = (time.monotonic(), 0, 0)
        now = time.monotonic()
        last, a, t = self.rate_last
        if now - last >= 0.5:
            self.rates = (
                (stats.acquired - a) / (now - last),
                (stats.tracked - t) / (now - last),
            )
            self.rate_last = (now, stats.acquired, stats.tracked)
        backlog = int(stats.enqueued - stats.written)
        recording = (
            "FAULT — INCOMPLETE"
            if stats.record_fault
            else (
                f"{int(stats.written)} frames · buffer {backlog}"
                if state.directory
                else "off"
            )
        )
        if stats.log_fault:
            recording += " · LOG INCOMPLETE"
        fault = bool(stats.capture_fault or stats.tracking_fault or stats.record_fault or stats.log_fault)
        if fault:
            self._error_text = state.message or "Acquisition, tracking, or recording error"
            self._error_until = float("inf")
        elif self._error_until == float("inf"):
            self._error_until = now + 0.25
        metrics_text = (
            f"ACQ {self.rates[0]:5.1f} fps  TRACK {self.rates[1]:5.1f} fps  "
            f"PROC {stats.processing_us / 1000:6.2f} ms  "
            f"SKIPPED {int(stats.tracking_skips):6d}  VIDEO {recording}"
        )
        self.metrics.setToolTip(metrics_text)
        self.metrics.setText(self.metrics.fontMetrics().elidedText(
            metrics_text, C.Qt.ElideRight, self.metrics.width() - 2
        ))
        self.update_alert()
        self.timeline.setMaximum(max(1, state.source.total - 1))
        if self.display_pause.isChecked():
            return
        payload = self.service.preview()
        fresh_payload = payload is not None
        if payload:
            self.last_payload = payload
        elif self.last_payload is None:
            return
        else:
            payload = self.last_payload
        r = payload["result"]
        full_params = payload["tracking"].model_copy(
            update={"template_radius": self.parameters["template_radius"].spin.value()}
        )
        self.full.set_frame(
            payload["frame"],
            r,
            scale=payload["scale"],
            crosshairs=self.crosshairs.isChecked(),
            params=full_params,
            template=payload["template"],
            circle=self.circle.isChecked(),
            inset=self.inset.isChecked(),
        )
        self.eye.set_frame(
            payload["crop"],
            r,
            origin=r["roi"][:2],
            pupil_mask=self.pupil_mask.isChecked(),
            cr_mask=self.cr_mask.isChecked(),
            params=payload["tracking"],
            crosshairs=self.crosshairs.isChecked(),
            pupil_evidence=payload.get("pupil_evidence"),
        )
        self.track_label.setText(r["track_status"])
        if fresh_payload:
            reason = r.get("reject_reason", "")
            if reason:
                self._reason_text = reason
                self._reason_until = float("inf") if active else now + 0.25
            elif self._reason_until == float("inf"):
                self._reason_until = now + 0.25
            self.update_alert()
        self.template_label.setText(r["template_status"] or "No template selected")
        if video:
            source_index = payload["source_index"]
            self.timeline.setMaximum(max(self.timeline.maximum(), source_index))
            if not self.timeline.isSliderDown():
                self.timeline.setValue(source_index)
            self.frame_label.setText(f"Frame {source_index + 1}")
        else:
            self.frame_label.setText(f"Frame {payload['frame_id']}")
        if (
            self.navigation_pending is not None
            and payload.get("navigation_id", 0) >= self.navigation_pending
        ):
            self.navigation_pending = None
            self.full.setEnabled(not self.display_pause.isChecked())
            self.eye.setEnabled(not self.display_pause.isChecked())
            if self.queued_seek is not None:
                target, self.queued_seek = self.queued_seek, None
                self.deferred_video_action = None
                self.seek_video(target)
            elif self.deferred_video_action is not None:
                command, args = self.deferred_video_action
                self.deferred_video_action = None
                self.call(command, **args)
        tracking = payload["tracking"]
        signature = (
            tracking.tracking_mode,
            tracking.pupil_coordinates
            if tracking.tracking_mode is TrackingMode.PUPIL_ONLY
            else None,
            tracking.pupil_method,
        )
        if signature != self.output_signature:
            self.output_signature = signature
            self.history.clear()
            self.last_history_frame = None
            description = (
                "Pupil − CR"
                if signature[0] is not TrackingMode.PUPIL_ONLY
                else (
                    "ROI-relative pupil"
                    if signature[1] is PupilCoordinates.RELATIVE
                    else "Absolute pupil"
                )
            )
            self.trace.setTitle(description + " · pixels")
            self.xy.setTitle(description + " · X/Y")
        if (
            fresh_payload
            and payload["revision"] >= self.service.revision
            and not self.param_timer.isActive()
            and not self.pending
        ):
            for key, param in self.parameters.items():
                param.set_value(getattr(tracking, key))
            with C.QSignalBlocker(self.template_on):
                self.template_on.setChecked(tracking.template_tracking)
            with C.QSignalBlocker(self.pupil_method):
                self.pupil_method.setCurrentIndex(self.pupil_method.findData(tracking.pupil_method.value))
            self.update_method_controls()
        if payload["frame_id"] != self.last_history_frame:
            self.history.append((time.monotonic(), r["x"], r["y"]))
            self.last_history_frame = payload["frame_id"]
        if self.history:
            data = np.asarray(self.history)
            data = data[data[:, 0] > time.monotonic() - 8]
            times = data[:, 0] - time.monotonic()
            self.tx.setData(times, data[:, 1], connect="finite")
            self.ty.setData(times, data[:, 2], connect="finite")
            self.trace.setXRange(-8, 0, padding=0)
            valid = np.isfinite(data[:, 1]) & np.isfinite(data[:, 2])
            if valid.any():
                self.xyline.setData(data[:, 1], data[:, 2], connect="finite")
            else:
                self.xyline.setData([], [])
            if np.isfinite(r["x"]) and np.isfinite(r["y"]):
                self.xypoint.setData([r["x"]], [r["y"]])
            else:
                self.xypoint.setData([], [])

    def closeEvent(self, event):
        if self.service.run:
            if not self._closing:
                self._closing = True
                self.call("stop")
            event.ignore()
            C.QTimer.singleShot(100, self.close)
            return
        self.camera_name_discovery.stop()
        self.timer.stop()
        self.service.close()
        event.accept()
