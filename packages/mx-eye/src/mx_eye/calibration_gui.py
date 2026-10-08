"""Non-modal manual calibration workspace."""

from __future__ import annotations

import math
import time
from collections import deque

import pyqtgraph as pg
from PySide6 import QtCore as C
from PySide6 import QtWidgets as W

from .calibration import CalibrationProfile, CalibrationStore


class CalibrationWindow(W.QMainWindow):
    """Screen-space gaze preview and coarse manual transform controls."""

    profileChanged = C.Signal(object)

    def __init__(
        self,
        store: CalibrationStore,
        profile: CalibrationProfile | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.profile = profile or CalibrationProfile(animal_id="default")
        self._last_raw: tuple[float, float] | None = None
        self._history: deque[tuple[float, float, float]] = deque()
        self._loading = False
        self._screen_size: tuple[int, int] | None = None
        self.setWindowTitle("mx_eye · Calibration workspace")
        self.resize(1320, 760)
        self.setMinimumSize(900, 560)

        central = W.QWidget(self)
        self.setCentralWidget(central)
        layout = W.QHBoxLayout(central)
        layout.setContentsMargins(14, 14, 14, 14)

        left = W.QWidget()
        left_layout = W.QVBoxLayout(left)
        header = W.QHBoxLayout()
        self.screen_label = W.QLabel()
        header.addWidget(self.screen_label)
        header.addStretch()
        header.addWidget(W.QLabel("Trace history"))
        self.history_seconds = W.QComboBox()
        for seconds in (1, 5, 10, 15, 30):
            self.history_seconds.addItem(f"{seconds} s", seconds)
        self.history_seconds.addItem("All", None)
        self.history_seconds.setCurrentIndex(self.history_seconds.findData(10))
        self.history_seconds.currentIndexChanged.connect(self._redraw)
        header.addWidget(self.history_seconds)
        left_layout.addLayout(header)

        zoom_out = W.QToolButton()
        zoom_out.setText("−")
        zoom_out.setToolTip("Zoom out")
        zoom_out.clicked.connect(lambda: self._zoom(1.25))
        header.addWidget(zoom_out)
        self.zoom_label = W.QLabel("100%")
        self.zoom_label.setMinimumWidth(52)
        self.zoom_label.setAlignment(C.Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.zoom_label)
        zoom_in = W.QToolButton()
        zoom_in.setText("+")
        zoom_in.setToolTip("Zoom in")
        zoom_in.clicked.connect(lambda: self._zoom(0.8))
        header.addWidget(zoom_in)
        fit = W.QPushButton("Fit screen")
        fit.clicked.connect(self._fit_screen)
        header.addWidget(fit)

        self.plot = pg.PlotWidget(background="#4b535d")
        self.plot.setMenuEnabled(False)
        self.plot.hideButtons()
        self.plot.setLabel("bottom", "Physical screen X", units="px")
        self.plot.setLabel("left", "Physical screen Y", units="px")
        view = self.plot.getViewBox()
        view.setAspectLocked(True)
        view.invertY(True)
        view.sigRangeChanged.connect(self._update_zoom_label)
        self.screen_page = W.QGraphicsRectItem()
        self.screen_page.setPen(pg.mkPen("#65717e", width=3))
        self.screen_page.setBrush(pg.mkBrush("#151a20"))
        self.screen_page.setZValue(-10)
        self.plot.addItem(self.screen_page)
        self.trace = pg.ScatterPlotItem(size=5, pen=None)
        self.current = pg.ScatterPlotItem(
            size=15,
            pen=pg.mkPen("#ffffff", width=2),
            brush=pg.mkBrush("#ff4057"),
        )
        self.plot.addItem(self.trace)
        self.plot.addItem(self.current)
        left_layout.addWidget(self.plot, 1)
        layout.addWidget(left, 3)

        controls = W.QGroupBox("Manual calibration")
        form = W.QFormLayout(controls)
        self.animal = W.QLineEdit()
        self.animal.editingFinished.connect(self._load_animal)
        form.addRow("Animal ID", self.animal)
        self.width = self._integer(1, 32768)
        self.height = self._integer(1, 32768)
        form.addRow("Screen width", self.width)
        form.addRow("Screen height", self.height)
        self.offset_x = self._number(-32768, 32768, 2)
        self.offset_y = self._number(-32768, 32768, 2)
        form.addRow(
            "Offset X (px)", self._with_slider(self.offset_x, -32768, 32768, 1)
        )
        form.addRow(
            "Offset Y (px)", self._with_slider(self.offset_y, -32768, 32768, 1)
        )
        self.gain_x = self._number(-200, 200, 3)
        self.gain_y = self._number(-200, 200, 3)
        form.addRow("Gain X", self._with_slider(self.gain_x, -200, 200, 100))
        form.addRow("Gain Y", self._with_slider(self.gain_y, -200, 200, 100))
        self.rotation = self._number(-180, 180, 2)
        form.addRow(
            "Rotation (deg)", self._with_slider(self.rotation, -180, 180, 10)
        )

        center = W.QPushButton("Set current eye position to screen centre")
        center.clicked.connect(self._set_current_as_center)
        form.addRow(center)
        reset = W.QPushButton("Reset transform")
        reset.clicked.connect(self._reset)
        form.addRow(reset)
        save = W.QPushButton("Save and activate")
        save.setObjectName("primary")
        save.clicked.connect(self._save)
        form.addRow(save)
        self.status = W.QLabel()
        self.status.setWordWrap(True)
        self.status.setSizePolicy(
            W.QSizePolicy.Policy.Ignored,
            W.QSizePolicy.Policy.Preferred,
        )
        form.addRow(self.status)
        note = W.QLabel(
            "Changes are previewed live. Saving creates a new immutable profile; "
            "raw pupil and corneal-reflection coordinates remain recorded."
        )
        note.setWordWrap(True)
        form.addRow(note)
        layout.addWidget(controls, 1)

        self._editors = (
            self.width,
            self.height,
            self.offset_x,
            self.offset_y,
            self.gain_x,
            self.gain_y,
            self.rotation,
        )
        for editor in self._editors:
            editor.valueChanged.connect(self._controls_changed)
        self._load_profile(self.profile)

    @staticmethod
    def _with_slider(
        box: W.QDoubleSpinBox, low: int, high: int, scale: int
    ) -> W.QWidget:
        widget = W.QWidget()
        layout = W.QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        slider = W.QSlider(C.Qt.Orientation.Horizontal)
        slider.setRange(low * scale, high * scale)
        slider.setValue(round(box.value() * scale))
        slider.valueChanged.connect(lambda value: box.setValue(value / scale))
        box.valueChanged.connect(lambda value: slider.setValue(round(value * scale)))
        layout.addWidget(slider, 1)
        layout.addWidget(box)
        return widget

    @staticmethod
    def _number(low: float, high: float, decimals: int) -> W.QDoubleSpinBox:
        box = W.QDoubleSpinBox()
        box.setRange(low, high)
        box.setDecimals(decimals)
        box.setKeyboardTracking(False)
        return box

    @staticmethod
    def _integer(low: int, high: int) -> W.QSpinBox:
        box = W.QSpinBox()
        box.setRange(low, high)
        box.setKeyboardTracking(False)
        return box

    def _load_profile(self, profile: CalibrationProfile) -> None:
        self._loading = True
        self.profile = profile
        self.animal.setText(profile.animal_id)
        values = (
            profile.screen_width,
            profile.screen_height,
            profile.offset_x,
            profile.offset_y,
            profile.gain_x,
            profile.gain_y,
            profile.rotation_deg,
        )
        for editor, value in zip(self._editors, values, strict=True):
            editor.setValue(value)
        self._loading = False
        self.status.setText(
            f"Active · {profile.created_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self._update_screen(fit=True)
        self.profileChanged.emit(profile)

    def _profile_from_controls(self) -> CalibrationProfile:
        animal = self.animal.text().strip() or "default"
        data = self.profile.model_dump()
        data.update(
            animal_id=animal,
            screen_width=self.width.value(),
            screen_height=self.height.value(),
            offset_x=self.offset_x.value(),
            offset_y=self.offset_y.value(),
            gain_x=self.gain_x.value(),
            gain_y=self.gain_y.value(),
            rotation_deg=self.rotation.value(),
        )
        return CalibrationProfile.model_validate(data)

    def _controls_changed(self, *_args) -> None:
        if self._loading:
            return
        try:
            self.profile = self._profile_from_controls()
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.status.setText("Unsaved changes · live preview active")
        self._update_screen()
        self._redraw()
        self.profileChanged.emit(self.profile)

    def _update_screen(self, *, fit: bool = False) -> None:
        self.screen_label.setText(
            f"Calibrated gaze · {self.profile.screen_width} × "
            f"{self.profile.screen_height} screen pixels · view rotated 90° CCW"
        )
        size = (self.profile.screen_width, self.profile.screen_height)
        size_changed = size != self._screen_size
        self._screen_size = size
        display_width, display_height = self._display_size()
        self.screen_page.setRect(0, 0, display_width, display_height)
        if fit or size_changed:
            self._fit_screen()

    def _fit_screen(self) -> None:
        display_width, display_height = self._display_size()
        self.plot.getViewBox().setRange(
            C.QRectF(0, 0, display_width, display_height),
            padding=0.12,
        )

    def _display_size(self) -> tuple[int, int]:
        """Return physical-view dimensions after a 90° counterclockwise turn."""
        return self.profile.screen_height, self.profile.screen_width

    def _display_point(self, x: float, y: float) -> tuple[float, float]:
        """Rotate pygame screen coordinates for the physically oriented view."""
        return y, self.profile.screen_width - x

    def _zoom(self, factor: float) -> None:
        self.plot.getViewBox().scaleBy((factor, factor))

    def _update_zoom_label(self, *_args) -> None:
        x_range, y_range = self.plot.getViewBox().viewRange()
        visible_width = abs(x_range[1] - x_range[0])
        visible_height = abs(y_range[1] - y_range[0])
        if not visible_width or not visible_height:
            return
        display_width, display_height = self._display_size()
        zoom = min(
            display_width / visible_width,
            display_height / visible_height,
        )
        self.zoom_label.setText(f"{zoom * 100:.0f}%")

    def _load_animal(self) -> None:
        animal = self.animal.text().strip()
        if not animal:
            return
        try:
            profile = self.store.load_active(animal)
        except (OSError, TypeError, ValueError) as exc:
            self.status.setText(f"Cannot load calibration: {exc}")
            return
        if profile is None:
            data = self._profile_from_controls().model_dump()
            data["animal_id"] = animal
            self.profile = CalibrationProfile.model_validate(data)
            self.status.setText("No saved profile for this animal")
            self.profileChanged.emit(self.profile)
        else:
            self._history.clear()
            self._load_profile(profile)

    def _set_current_as_center(self) -> None:
        if self._last_raw is None:
            self.status.setText("No valid eye position is available yet")
            return
        raw_x, raw_y = self._last_raw
        theta = math.radians(self.rotation.value())
        cosine, sine = math.cos(theta), math.sin(theta)
        scaled_x = raw_x * self.gain_x.value()
        scaled_y = raw_y * self.gain_y.value()
        self.offset_x.setValue(-(cosine * scaled_x - sine * scaled_y))
        self.offset_y.setValue(-(sine * scaled_x + cosine * scaled_y))
        self._controls_changed()

    def _reset(self) -> None:
        self.offset_x.setValue(0)
        self.offset_y.setValue(0)
        self.gain_x.setValue(1)
        self.gain_y.setValue(1)
        self.rotation.setValue(0)
        self._controls_changed()

    def _save(self) -> None:
        try:
            candidate = self._profile_from_controls()
            path = self.store.save_and_activate(candidate)
            saved = CalibrationProfile.model_validate_json(path.read_text("utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            W.QMessageBox.warning(self, "Cannot save calibration", str(exc))
            return
        self._load_profile(saved)
        self.status.setText(
            f"Saved and active · "
            f"{saved.created_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')}"
        )

    def update_sample(self, x: float, y: float) -> None:
        if not (math.isfinite(x) and math.isfinite(y)):
            return
        self._last_raw = (x, y)
        self._history.append((time.monotonic(), x, y))
        self._redraw()

    def _redraw(self, *_args) -> None:
        if not self._history:
            self.trace.setData([], [])
            self.current.setData([], [])
            return
        seconds = self.history_seconds.currentData()
        if seconds is None:
            points = list(self._history)
        else:
            cutoff = time.monotonic() - seconds
            points = [point for point in self._history if point[0] >= cutoff]
        if not points:
            self.trace.setData([], [])
            self.current.setData([], [])
            return
        displayed_points = [
            self._display_point(*self.profile.apply(point[1], point[2]))
            for point in points
        ]
        count = len(displayed_points)
        brushes = [
            pg.mkBrush(255, 64, 87, max(18, round(210 * (i + 1) / count)))
            for i in range(count)
        ]
        self.trace.setData(
            [point[0] for point in displayed_points],
            [point[1] for point in displayed_points],
            brush=brushes,
        )
        self.current.setData(
            [displayed_points[-1][0]], [displayed_points[-1][1]]
        )
