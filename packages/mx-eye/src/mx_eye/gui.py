"""Desktop tracker window and dialogs. Run the tracker with python -m mx_eye."""

import base64
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore as C
from PySide6 import QtWidgets as W

from . import config as cfg
from .cameras import CameraControls
from .config import (
    PupilCoordinates,
    RecordingCodec,
    SourceMode,
    TrackingMode,
    Transport,
)
from .helptext import TIPS, add_tooltips
from .service import Service
from .widgets import EyeView, Parameter, Section, SeekSlider, label

ENUM_FIELDS = {
    ("network", "transport"): Transport,
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
                    (
                        "transport",
                        "Sample transport",
                        [item.value for item in Transport],
                    ),
                    ("data_port", "Sample port", 1024, 65535),
                    ("control_port", "Command port", 1024, 65535),
                    ("sync_port", "Clock-sync port", 1024, 65535),
                    ("udp_host", "UDP receiver address", None),
                ],
            ),
            "Recording": (
                "recording",
                [
                    ("directory", "Output folder", None),
                    ("buffer_mb", "Buffer size (MiB)", 8, 2048),
                    ("codec", "Codec", [item.value for item in RecordingCodec]),
                    ("record_simulation", "Record simulation", True),
                ],
            ),
        }
        notes = {
            "Camera": "FPS and format are requests to the driver. The status bar shows measured acquisition rate. Camera mode always records while running.",
            "Network": "For another computer, bind to 0.0.0.0 and use this tracker’s IP in the SDK. UDP sends to one configured receiver. Control is unauthenticated: use only your trusted local network.",
            "Recording": "MJPG: fast, lossy AVI. FFV1: lossless MKV, higher CPU demand. Full source frames are saved without overlays. Buffer overflow stops recording and marks the session incomplete; tracking continues.",
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
                        W.QDoubleSpinBox() if key in ("fps", "speed") else W.QSpinBox()
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
            self.config.value.source = self.camera_controls.values()
            for (group, key), widget in self.fields.items():
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
        central = W.QWidget()
        self.setCentralWidget(central)
        layout = W.QVBoxLayout(central)
        layout.setContentsMargins(16, 12, 16, 10)
        toolbar = W.QHBoxLayout()
        toolbar.addWidget(label("mx_eye", "brand"))
        toolbar.addWidget(label("MXBI  /  PUPIL + CR", "muted"))
        toolbar.addStretch()
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
        toolbar.addWidget(self.source)
        self.open_button = W.QPushButton("Open video…")
        self.open_button.clicked.connect(self.open_video)
        toolbar.addWidget(self.open_button)
        self.start_button = W.QPushButton("Start")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self.start)
        toolbar.addWidget(self.start_button)
        self.stop_button = W.QPushButton("Stop")
        self.stop_button.clicked.connect(lambda: self.call("stop"))
        toolbar.addWidget(self.stop_button)
        self.settings_button = W.QPushButton("Settings")
        self.settings_button.clicked.connect(self.settings)
        toolbar.addWidget(self.settings_button)
        layout.addLayout(toolbar)
        files = W.QHBoxLayout()
        for title, callback in [
            ("Load config…", self.load_config),
            ("Save config…", self.save_config),
            ("Load template…", self.load_template),
            ("Save template…", self.save_template),
        ]:
            button = W.QPushButton(title)
            button.clicked.connect(callback)
            files.addWidget(button)
        files.addStretch()
        camera_button = W.QPushButton("Camera settings…")
        camera_button.clicked.connect(self.settings)
        self.camera_settings_button = camera_button
        files.addWidget(camera_button)
        self.camera = W.QSpinBox()
        self.camera.setRange(0, 99)
        self.camera.setValue(config.value.source.camera)
        self.requested_fps = label(
            f"Requested {config.value.source.fps:g} fps", "muted"
        )
        files.addWidget(self.requested_fps)
        layout.addLayout(files)
        self.source_label = label(
            self.saved_source_path
            or "Live camera · full video is recorded during each session",
            "muted",
        )
        self.source_label.setTextInteractionFlags(C.Qt.TextSelectableByMouse)
        self.source_label.setWordWrap(True)
        layout.addWidget(self.source_label)
        self.metrics = label(
            "ACQUISITION  —       TRACKING  —       PROCESSING  —       RECORDING  —",
            "metric",
        )
        self.metrics.setWordWrap(True)
        layout.addWidget(self.metrics)
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
                header.addWidget(box)
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
        self.timer.start(40)
        add_tooltips(self)
        W.QApplication.instance().installEventFilter(self)

    def source_mode(self):
        return SourceMode(self.source.currentData())

    def tracking_mode(self):
        return TrackingMode(self.mode.currentText())

    def pupil_coordinates(self):
        return PupilCoordinates(self.coordinates.currentData())

    def call(self, command, callback=None, **args):
        self.pending.append((self.service.submit(command, **args), callback))

    def schedule_parameters(self, *args):
        if hasattr(self, "param_timer"):
            self.param_timer.start(80)

    def send_parameters(self):
        tracking = self.service.config.value.tracking.model_copy(deep=True)
        for key, param in self.parameters.items():
            setattr(tracking, key, param.spin.value())
        tracking.tracking_mode = self.tracking_mode()
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
        config.value.source.speed = self.speed.value()
        for key, param in self.parameters.items():
            setattr(config.value.tracking, key, param.spin.value())
        config.value.tracking.tracking_mode = self.tracking_mode()
        config.value.tracking.pupil_coordinates = self.pupil_coordinates()
        config.value.tracking.template_tracking = self.template_on.isChecked()
        self.call("settings", config=config, callback=lambda: self.call("start"))

    def settings(self):
        config = cfg.MxEyeConfigStore(self.service.config.value.model_copy(deep=True))
        config.value.source.camera = self.camera.value()
        dialog = Settings(config, self)
        if dialog.exec() == W.QDialog.Accepted:
            self.camera.setValue(dialog.config.value.source.camera)
            self.requested_fps.setText(
                f"Requested {dialog.config.value.source.fps:g} fps"
            )
            self.call("settings", config=dialog.config)

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
            config.value.source.mode = self.source_mode()
            config.value.source.path = self.saved_source_path
            config.value.source.camera = self.camera.value()
            config.value.source.speed = self.speed.value()
            for key, param in self.parameters.items():
                setattr(config.value.tracking, key, param.spin.value())
            config.value.tracking.tracking_mode = self.tracking_mode()
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
                    self.full.setEnabled(True)
                    self.eye.setEnabled(True)
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
        self.source_label.setText(
            self.saved_source_path
            if mode is SourceMode.VIDEO
            else (
                "Artificial eye · no camera required"
                if mode is SourceMode.SIMULATION
                else "Live camera · full video is recorded during each session"
            )
        )
        if mode is SourceMode.CAMERA and state.source.get("width"):
            info = state.source
            name = f"Camera {self.camera.value()}"
            camera_mode = f"{info['width']} × {info['height']} · {info.get('actual_format', 'unknown format')}"
            if info.get("driver_fps") is not None:
                camera_mode += f" · driver {info['driver_fps']:g} fps"
            self.source_label.setText(
                f"{name} · {camera_mode} · full video is recorded"
            )
        active = state.state in ("running", "starting", "stopping")
        for widget in (
            self.start_button,
            self.source,
            self.settings_button,
            self.camera_settings_button,
            self.camera,
        ):
            widget.setEnabled(not active and not self.pending)
        self.open_button.setEnabled(not self.pending and not self._closing)
        self.stop_button.setEnabled(active)
        video = active and self.service.config.value.source.mode is SourceMode.VIDEO
        if not video:
            self.navigation_pending = None
            self.queued_seek = None
            self.deferred_video_action = None
            self.full.setEnabled(True)
            self.eye.setEnabled(True)
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
                (stats.get("acquired", 0) - a) / (now - last),
                (stats.get("tracked", 0) - t) / (now - last),
            )
            self.rate_last = (now, stats.get("acquired", 0), stats.get("tracked", 0))
        backlog = int(stats.get("enqueued", 0) - stats.get("written", 0))
        recording = (
            "FAULT — INCOMPLETE"
            if stats.get("record_fault")
            else (
                f"{int(stats.get('written', 0))} frames · buffer {backlog}"
                if state.directory
                else "off"
            )
        )
        if stats.get("log_fault"):
            recording += " · LOG INCOMPLETE"
        self.metrics.setText(
            f"ACQ  {self.rates[0]:.1f} fps     TRACK  {self.rates[1]:.1f} fps     PROC  {stats.get('processing_us', 0) / 1000:.2f} ms     SKIPPED  {int(stats.get('tracking_skips', 0))}     VIDEO  {recording}"
        )
        self.metrics.setStyleSheet(
            "color:#ff817f;"
            if stats.get("record_fault") or stats.get("log_fault")
            else ""
        )
        self.timeline.setMaximum(max(1, state.source.get("total", 1) - 1))
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
        )
        self.track_label.setText(r["track_status"])
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
            self.full.setEnabled(True)
            self.eye.setEnabled(True)
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
        self.timer.stop()
        self.service.close()
        event.accept()
