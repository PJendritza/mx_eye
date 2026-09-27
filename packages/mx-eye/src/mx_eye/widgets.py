"""Qt widgets used only by the desktop apps."""

import math

import cv2
import numpy as np
from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

from .config import TrackingMode

STYLE = """
QWidget { background:#10151d; color:#dce6f1; font-family:Inter,Segoe UI,sans-serif; font-size:12px; }
QMainWindow,QDialog { background:#10151d; }
QLabel#brand { font-size:23px; font-weight:700; color:#f2f7ff; }
QLabel#muted { color:#8e9caf; }
QLabel#metric { padding:8px 13px; background:#192331; border-radius:6px; }
QPushButton,QToolButton { background:#233145; border:1px solid #34455b; border-radius:4px; padding:4px 7px; }
QPushButton:hover,QToolButton:hover { background:#30445e; }
QPushButton:disabled { color:#596a80; background:#18212d; }
QPushButton#primary { background:#267d68; border-color:#389982; font-weight:600; }
QLineEdit,QSpinBox,QDoubleSpinBox,QComboBox { background:#182330; border:1px solid #34455b; border-radius:3px; padding:2px; }
QScrollArea { border:0; }
QSlider::groove:horizontal { background:#2a3b50; height:4px; border-radius:2px; }
QSlider::handle:horizontal { background:#61b6ef; width:12px; margin:-5px 0; border-radius:6px; }
QTabBar::tab { background:#182330; padding:8px 15px; }
QTabBar::tab:selected { background:#2b405b; }
QCheckBox { spacing:7px; }
QSplitter::handle { background:#253345; }
QToolTip { background:#233145; color:#eaf3ff; border:1px solid #536b89; }
"""


def label(text, name=None):
    w = W.QLabel(text)
    if name:
        w.setObjectName(name)
    return w


class SeekSlider(W.QSlider):
    seek = C.Signal(int)

    def __init__(self):
        super().__init__(C.Qt.Horizontal)
        self.last_seek = None
        self.sliderReleased.connect(self.finish_seek)

    def finish_seek(self):
        if self.value() != self.last_seek:
            self.seek.emit(self.value())
            self.last_seek = self.value()

    def position_value(self, event):
        option = W.QStyleOptionSlider()
        self.initStyleOption(option)
        handle = self.style().subControlRect(
            W.QStyle.CC_Slider, option, W.QStyle.SC_SliderHandle, self
        )
        groove = self.style().subControlRect(
            W.QStyle.CC_Slider, option, W.QStyle.SC_SliderGroove, self
        )
        return W.QStyle.sliderValueFromPosition(
            self.minimum(),
            self.maximum(),
            round(event.position().x() - groove.x() - handle.width() / 2),
            max(1, groove.width() - handle.width()),
            option.upsideDown,
        )

    def mousePressEvent(self, event):
        if event.button() == C.Qt.LeftButton:
            self.setFocus()
            self.setSliderDown(True)
            self.setValue(self.position_value(event))
            self.last_seek = self.value()
            self.seek.emit(self.value())
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.isSliderDown():
            self.setValue(self.position_value(event))
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == C.Qt.LeftButton and self.isSliderDown():
            self.setValue(self.position_value(event))
            self.setSliderDown(False)
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class Section(W.QWidget):
    def __init__(self, title, opened=True):
        super().__init__()
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 2)
        layout.setSpacing(2)
        button = W.QToolButton(text=title)
        button.setToolButtonStyle(C.Qt.ToolButtonTextBesideIcon)
        button.setCheckable(True)
        button.setToolTip("Click to expand or collapse " + title.lower() + " controls.")
        button.setChecked(opened)
        button.setSizePolicy(W.QSizePolicy.Expanding, W.QSizePolicy.Fixed)
        self.content = W.QWidget()
        self.body = W.QVBoxLayout(self.content)
        self.body.setContentsMargins(5, 3, 5, 4)
        self.body.setSpacing(2)
        layout.addWidget(button)
        layout.addWidget(self.content)

        def toggle(checked):
            self.content.setVisible(checked)
            button.setArrowType(C.Qt.DownArrow if checked else C.Qt.RightArrow)

        button.toggled.connect(toggle)
        toggle(opened)


class Parameter(W.QWidget):
    changed = C.Signal()

    def __init__(self, title, value, minimum, maximum, step=1):
        super().__init__()
        self.minimum, self.step = minimum, step
        layout = W.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        caption = label(title)
        caption.setFixedWidth(105)
        caption.setToolTip(title)
        layout.addWidget(caption)
        self.spin = W.QDoubleSpinBox()
        self.spin.setRange(minimum, maximum)
        self.spin.setDecimals(2 if step < 1 else 0)
        self.spin.setSingleStep(step)
        self.spin.setValue(value)
        self.spin.setFixedWidth(62)
        self.spin.setKeyboardTracking(False)
        self.slider = W.QSlider(C.Qt.Horizontal)
        self.slider.setRange(0, round((maximum - minimum) / step))
        self.slider.setValue(round((value - minimum) / step))
        self.slider.valueChanged.connect(
            lambda v: self.spin.setValue(minimum + v * step)
        )
        self.spin.valueChanged.connect(self._value)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.spin)

    def _value(self, value):
        with C.QSignalBlocker(self.slider):
            self.slider.setValue(round((value - self.minimum) / self.step))
        self.changed.emit()

    def set_value(self, value):
        if self.slider.isSliderDown() or self.spin.hasFocus():
            return
        with C.QSignalBlocker(self.spin), C.QSignalBlocker(self.slider):
            self.spin.setValue(value)
            self.slider.setValue(round((value - self.minimum) / self.step))


class EyeView(W.QWidget):
    action = C.Signal(str, object)

    def __init__(self, crop=False):
        super().__init__()
        self.crop = crop
        self.image = None
        self.result = {}
        self.origin = (0, 0)
        self.image_scale = 1
        self.display_scale = 1
        self.offset = (0, 0)
        self.drag = None
        self.drag_preview = None
        self.crosshairs = True
        self.template_image = None
        self.template_radius = 0
        self.circle = True
        self.inset = True
        self.area_limit = None
        self.zoom = 1.0
        self.zoom_center = (0.5, 0.5)
        self.zoom_reset = None
        if not crop:
            self.zoom_reset = W.QPushButton("Reset", self)
            self.zoom_reset.setToolTip("Show the complete source image at 100% zoom.")
            self.zoom_reset.setFixedSize(49, 25)
            self.zoom_reset.move(86, 8)
            self.zoom_reset.clicked.connect(self.reset_zoom)
            self.zoom_reset.hide()
        self.area_timer = C.QTimer(self)
        self.area_timer.setSingleShot(True)
        self.area_timer.timeout.connect(self.hide_area_limit)
        self.setMinimumSize(210, 160)
        self.setSizePolicy(W.QSizePolicy.Expanding, W.QSizePolicy.Expanding)
        self.setToolTip(
            "Left click: pupil · Right click: CR · Shift-click: template"
            if crop
            else "Drag: move ROI · Corner drag: resize · Shift-drag: draw ROI · Right click: template · Drag magenta border: move search window · Wheel: zoom source view"
        )

    def reset_zoom(self):
        self.zoom = 1.0
        self.zoom_center = (0.5, 0.5)
        if self.zoom_reset is not None:
            self.zoom_reset.hide()
        self.update()

    def view_geometry(self):
        if self.image is None:
            return (0, 0)
        fit = min(
            self.width() / self.image.width(), self.height() / self.image.height()
        )
        iw, ih = (
            self.image.width() * fit * self.zoom,
            self.image.height() * fit * self.zoom,
        )
        cx, cy = self.zoom_center
        cx = (
            0.5
            if iw <= self.width()
            else max(self.width() / (2 * iw), min(1 - self.width() / (2 * iw), cx))
        )
        cy = (
            0.5
            if ih <= self.height()
            else max(self.height() / (2 * ih), min(1 - self.height() / (2 * ih), cy))
        )
        self.zoom_center = (cx, cy)
        self.offset = (self.width() / 2 - cx * iw, self.height() / 2 - cy * ih)
        self.display_scale = fit * self.zoom * self.image_scale
        return iw, ih

    def show_area_limit(self, key, area):
        self.area_limit = (key, max(0, float(area)))
        self.area_timer.start(3000)
        self.update()

    def hide_area_limit(self):
        self.area_limit = None
        self.update()

    def set_frame(
        self,
        frame,
        result,
        scale=1,
        origin=(0, 0),
        masks=False,
        params=None,
        crosshairs=True,
        pupil_mask=None,
        cr_mask=None,
        template=None,
        circle=True,
        inset=True,
    ):
        frame = np.ascontiguousarray(frame.copy())
        pupil_mask = masks if pupil_mask is None else pupil_mask
        cr_mask = masks if cr_mask is None else cr_mask
        if (pupil_mask or cr_mask) and params:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            for mask, color in [
                ((gray < params.pupil_thr) & pupil_mask, (245, 135, 65)),
                (
                    (gray > params.cr_thr)
                    & cr_mask
                    & (params.tracking_mode is not TrackingMode.PUPIL_ONLY),
                    (80, 80, 245),
                ),
            ]:
                frame[mask] = (frame[mask] * 0.4 + np.array(color) * 0.6).astype(
                    np.uint8
                )
        previous_size = self.image.size() if self.image is not None else None
        self.image = G.QImage(
            frame.data,
            frame.shape[1],
            frame.shape[0],
            frame.strides[0],
            G.QImage.Format_BGR888,
        ).copy()
        if (
            not self.crop
            and previous_size is not None
            and previous_size != self.image.size()
        ):
            self.reset_zoom()
        self.result = result
        self.origin = origin
        self.image_scale = scale
        self.crosshairs = crosshairs
        self.circle, self.inset = circle, inset
        self.template_radius = params.template_radius if params is not None else 0
        self.template_image = None
        if template is not None:
            gray = np.ascontiguousarray(template, dtype=np.uint8)
            self.template_image = G.QImage(
                gray.data,
                gray.shape[1],
                gray.shape[0],
                gray.strides[0],
                G.QImage.Format_Grayscale8,
            ).copy()
        self.update()

    def _point(self, xy):
        return C.QPointF(
            self.offset[0] + (xy[0] - self.origin[0]) * self.display_scale,
            self.offset[1] + (xy[1] - self.origin[1]) * self.display_scale,
        )

    def _source(self, event):
        if self.image is None:
            return None
        self.view_geometry()
        p = event.position()
        x = (p.x() - self.offset[0]) / self.display_scale
        y = (p.y() - self.offset[1]) / self.display_scale
        if not (
            0 <= x < self.image.width() / self.image_scale
            and 0 <= y < self.image.height() / self.image_scale
        ):
            return None
        return (x + self.origin[0], y + self.origin[1])

    def paintEvent(self, event):
        painter = G.QPainter(self)
        painter.fillRect(self.rect(), G.QColor("#0a0e14"))
        if self.image is None:
            painter.setPen(G.QColor("#68798e"))
            painter.drawText(
                self.rect(),
                C.Qt.AlignCenter,
                "Eye ROI" if self.crop else "Choose a source and press Start",
            )
            return
        iw, ih = self.view_geometry()
        painter.drawImage(C.QRectF(*self.offset, iw, ih), self.image)
        painter.setRenderHint(G.QPainter.Antialiasing)
        if not self.crop and self.zoom > 1.001:
            painter.fillRect(C.QRectF(8, 8, 74, 25), G.QColor(0, 0, 0, 200))
            painter.setPen(G.QColor("#ffffff"))
            painter.drawText(
                C.QRectF(8, 8, 74, 25),
                C.Qt.AlignCenter,
                f"{round(self.zoom * 100)}% zoom",
            )
        if not self.crop:
            x, y, w, h = self.result.get("roi", [0, 0, 0, 0])
            painter.setPen(G.QPen(G.QColor("#eac768"), 1.6))
            a, b = self._point((x, y)), self._point((x + w, y + h))
            painter.drawRect(C.QRectF(a, b))
            painter.fillRect(C.QRectF(b.x() - 4, b.y() - 4, 8, 8), G.QColor("#eac768"))
            search = self.result.get("search_rect")
            if search:
                painter.setPen(G.QPen(G.QColor("#d777df"), 1.5, C.Qt.DashLine))
                painter.drawRect(
                    C.QRectF(self._point(search[:2]), self._point(search[2:]))
                )
            center = self.result.get("template_center")
            if self.circle and center is not None:
                painter.setPen(G.QPen(G.QColor("#d777df"), 1.5))
                radius = self.template_radius * self.display_scale
                painter.drawEllipse(self._point(center), radius, radius)
        if self.drag_preview:
            x, y, w, h = self.drag_preview
            painter.setPen(G.QPen(G.QColor("#f7d987"), 2, C.Qt.DashLine))
            painter.drawRect(C.QRectF(self._point((x, y)), self._point((x + w, y + h))))
        if self.crosshairs:
            for key, color in [("pupil", "#67d5f0"), ("cr", "#ffab62")]:
                candidate = self.result.get(key)
                if candidate:
                    p = self._point((candidate["x"], candidate["y"]))
                    painter.setPen(G.QPen(G.QColor(color), 1.5))
                    painter.drawLine(p + C.QPointF(-8, 0), p + C.QPointF(8, 0))
                    painter.drawLine(p + C.QPointF(0, -8), p + C.QPointF(0, 8))
        if not self.crop and self.inset and self.template_image is not None:
            size = min(110, max(55, min(iw, ih) * 0.22))
            x = max(8, min(self.width() - size - 12, self.offset[0] + iw - size - 10))
            y = max(25, min(self.height() - size - 12, self.offset[1] + ih - size - 10))
            painter.fillRect(
                C.QRectF(x - 4, y - 21, size + 8, size + 25), G.QColor("#10151d")
            )
            painter.drawImage(C.QRectF(x, y, size, size), self.template_image)
            painter.setPen(G.QPen(G.QColor("#d777df"), 1))
            painter.drawRect(C.QRectF(x, y, size, size))
            painter.drawText(C.QPointF(x, y - 6), "Template")
        if self.crop and self.area_limit is not None:
            key, area = self.area_limit
            feature, limit = key.split("_")
            color = G.QColor("#4187f5" if feature == "pupil" else "#f55050")
            radius = math.sqrt(area / math.pi) * self.display_scale
            caption = f"{'Pupil' if feature == 'pupil' else 'CR'} {'min' if limit == 'min' else 'max'} · {area:g} px²"
            preferred = max(
                110,
                2 * radius + 44,
                painter.fontMetrics().horizontalAdvance(caption) + 16,
            )
            side = min(preferred, max(1, iw - 16), max(1, ih - 16))
            x, y = self.offset[0] + 8, self.offset[1] + ih - side - 8
            box = C.QRectF(x, y, side, side)
            painter.save()
            painter.fillRect(box, G.QColor(0, 0, 0, 204))
            painter.setClipRect(box.adjusted(4, 4, -4, -24))
            painter.setPen(C.Qt.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(
                C.QPointF(x + side / 2, y + (side - 24) / 2), radius, radius
            )
            painter.setClipping(False)
            painter.setPen(color)
            painter.drawText(
                box.adjusted(6, 0, -6, -5), C.Qt.AlignBottom | C.Qt.AlignLeft, caption
            )
            if 2 * radius > side - 32:
                painter.drawText(
                    box.adjusted(6, 4, -6, 0),
                    C.Qt.AlignTop | C.Qt.AlignLeft,
                    "Clipped · true scale",
                )
            painter.restore()

    def mousePressEvent(self, event):
        p = self._source(event)
        if p is None:
            return
        shift = bool(event.modifiers() & C.Qt.ShiftModifier)
        if self.crop:
            if shift:
                self.action.emit("template", dict(point=p))
            else:
                kind = "cr" if event.button() == C.Qt.RightButton else "pupil"
                self.action.emit("pick", dict(kind=kind, point=p))
            return
        if event.button() == C.Qt.RightButton:
            self.action.emit("template", dict(point=p))
            return
        roi = list(self.result.get("roi", [0, 0, 100, 100]))
        mode = "draw" if shift else "move"
        if (
            not shift
            and math.hypot(p[0] - roi[0] - roi[2], p[1] - roi[1] - roi[3])
            * self.display_scale
            < 15
        ):
            mode = "resize"
        search = self.result.get("search_rect")
        if search and not shift:
            a, b, c, d = search
            if (
                a - 10 <= p[0] <= c + 10
                and b - 10 <= p[1] <= d + 10
                and min(abs(p[0] - a), abs(p[0] - c), abs(p[1] - b), abs(p[1] - d))
                * self.display_scale
                < 10
            ):
                mode = "search"
        self.drag = (mode, p, roi, search)

    def mouseMoveEvent(self, event):
        p = self._source(event)
        if self.drag is None or p is None:
            return
        mode, old, roi, search = self.drag
        dx, dy = p[0] - old[0], p[1] - old[1]
        x, y, w, h = roi
        if mode == "draw":
            rect = [
                min(p[0], old[0]),
                min(p[1], old[1]),
                max(30, abs(dx)),
                max(30, abs(dy)),
            ]
        elif mode == "resize":
            rect = [x, y, max(30, w + dx), max(30, h + dy)]
        elif mode == "search":
            a, b, c, d = search
            rect = [a + dx, b + dy, c - a, d - b]
        else:
            rect = [x + dx, y + dy, w, h]
        self.drag_preview = rect
        self.update()

    def mouseReleaseEvent(self, event):
        self.drag_preview = None
        self.update()
        p = self._source(event)
        if self.drag is None or p is None:
            self.drag = None
            return
        mode, old, roi, search = self.drag
        self.drag = None
        dx, dy = p[0] - old[0], p[1] - old[1]
        if mode == "search":
            a, b, c, d = search
            self.action.emit(
                "search",
                dict(
                    center=((a + c) / 2 + dx, (b + d) / 2 + dy), size=max(c - a, d - b)
                ),
            )
            return
        if mode == "draw":
            roi = [
                min(p[0], old[0]),
                min(p[1], old[1]),
                max(30, abs(dx)),
                max(30, abs(dy)),
            ]
        elif mode == "resize":
            roi[2:] = [max(30, roi[2] + dx), max(30, roi[3] + dy)]
        else:
            roi[0] += dx
            roi[1] += dy
        self.action.emit("roi", dict(roi=roi))

    def wheelEvent(self, event):
        if self.crop or self.image is None:
            event.ignore()
            return
        self.view_geometry()
        position = event.position()
        px = (position.x() - self.offset[0]) / self.display_scale
        py = (position.y() - self.offset[1]) / self.display_scale
        factor = 1.2 ** (event.angleDelta().y() / 120)
        new_zoom = max(1.0, min(8.0, self.zoom * factor))
        if abs(new_zoom - 1) < 0.01:
            self.reset_zoom()
        elif new_zoom != self.zoom:
            fit = min(
                self.width() / self.image.width(), self.height() / self.image.height()
            )
            iw, ih = (
                self.image.width() * fit * new_zoom,
                self.image.height() * fit * new_zoom,
            )
            cx = (self.width() / 2 - position.x() + px * fit * new_zoom) / iw
            cy = (self.height() / 2 - position.y() + py * fit * new_zoom) / ih
            self.zoom = new_zoom
            self.zoom_center = (cx, cy)
            self.zoom_reset.show()
            self.zoom_reset.raise_()
            self.update()
        event.accept()
