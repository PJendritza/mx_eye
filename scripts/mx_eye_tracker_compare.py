"""Offline A/B pupil tracking. Run this file with MX Eye's Python environment.

Pass a video or an MX Eye recording folder, or use Open video in the window.
Requires the MX Eye GUI dependencies. Uses the installed tracker and video reader;
only the experimental pupil-blob calculation is implemented here.
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path
from threading import Event

import cv2
import numpy as np
import pyqtgraph as pg
from mx_eye.config import (
    PUPIL_METHODS,
    MxEyeConfigStore,
    PupilCoordinates,
    PupilMethod,
    TemplateConfig,
    TrackingConfig,
    TrackingMode,
)
from mx_eye.helptext import TIPS
from mx_eye.pipeline import restore_template, serialize_template
from mx_eye.tracking import Tracker
from mx_eye.video import VideoReader
from mx_eye.widgets import STYLE, EyeView, Parameter, Section, SeekSlider
from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

COLORS = {'A': '#6bc6ff', 'B': '#ffad69'}
TRACE_LIMIT = 600
TIMING_STAGES = (
    ('total', 'Total tracking'),
    ('template', 'Template following'),
    ('blur', 'Gaussian blur'),
    ('resize', 'Bilinear upsampling'),
    ('mask', 'Experimental threshold mask'),
    ('cleanup', 'Mask cleanup'),
    ('pupil', 'Pupil components and ellipse fit'),
    ('cr', 'CR components'),
    ('detection_other', 'Other detection and gates'),
    ('other', 'Other tracker work'),
)


@lru_cache(maxsize=32)
def disk(radius):
    y, x = np.ogrid[-radius:radius + 1, -radius:radius + 1]
    return (x * x + y * y <= radius * radius).astype(np.uint8)


class CompareTracker(Tracker):
    """Change pupil evidence and/or its center before MX Eye scores candidates."""

    center_of_mass = False
    sampling = 1
    blur = False
    blur_sigma = 0.6
    cleanup_radius = 0

    def __init__(self, config):
        super().__init__(config)
        self.timings = {}

    def process(self, *args, **kwargs):
        self.timings.clear()
        return super().process(*args, **kwargs)

    def timed(self, step, operation, *args, **kwargs):
        start = time.perf_counter_ns()
        result = operation(*args, **kwargs)
        self.timings[step] = self.timings.get(step, 0) + time.perf_counter_ns() - start
        return result

    def track_template(self):
        if self.config.template_tracking and self.template is not None:
            self.timed('template', super().track_template)

    def detect_pupil_cr(self, *args, **kwargs):
        return self.timed('detection', super().detect_pupil_cr, *args, **kwargs)

    def timing_sample(self, total):
        timings = self.timings
        measured_detection = sum(timings.get(key, 0) for key in ('mask', 'blur', 'resize', 'cleanup', 'pupil', 'cr'))
        timings['detection_other'] = max(0, timings['detection'] - measured_detection)
        timings['other'] = max(0, total - timings['detection'] - timings.get('template', 0))
        timings['total'] = total
        return tuple(timings.get(key, 0) / 1e6 for key, _ in TIMING_STAGES)

    def component_candidates(self, mask, min_area, max_area, roi_x, roi_y, kind):
        if kind != 'pupil':
            return self.timed('cr', super().component_candidates, mask, min_area, max_area, roi_x, roi_y, kind)
        threshold_mode = self.config.pupil_method is PupilMethod.THRESHOLD
        scale = self.sampling if threshold_mode else 1
        blur = threshold_mode and self.blur
        if not self.center_of_mass and scale == 1 and not blur:
            mask = self.clean_pupil_mask(mask, scale)
            return self.timed('pupil', super().component_candidates, mask, min_area, max_area, roi_x, roi_y, kind)

        if scale != 1 or blur:
            h, w = mask.shape
            gray = self.gray_region(roi_x, roi_y, roi_x + w, roi_y + h).astype(np.float32)
            if blur:
                radius = max(1, round(2 * self.blur_sigma))
                kernel = (2 * radius + 1, 2 * radius + 1)
                gray = self.timed('blur', cv2.GaussianBlur, gray, kernel, self.blur_sigma)
            if scale != 1:
                gray = self.timed('resize', cv2.resize, gray, None, fx=scale, fy=scale,
                                  interpolation=cv2.INTER_LINEAR)
            mask = self.timed('mask', cv2.compare, gray, self.config.pupil_thr, cv2.CMP_LT)
            self.pupil_evidence = mask

        mask = self.clean_pupil_mask(mask, scale)
        return self.timed('pupil', self.experimental_candidates, mask, min_area, max_area, roi_x, roi_y, scale)

    def experimental_candidates(self, mask, min_area, max_area, roi_x, roi_y, scale):
        _, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
        areas = stats[1:, cv2.CC_STAT_AREA] / (scale * scale)
        selected = np.flatnonzero((areas >= min_area) & (areas <= max_area)) + 1
        if not len(selected):
            self._area_rejections['pupil'] = (
                f'No pupil region within {min_area}–{max_area} px²' if len(areas)
                else 'No pupil pixels at current threshold'
            )
        candidates = []
        for label in selected:
            area = float(areas[label - 1])
            cx, cy = centroids[label]
            ellipse = None
            aspect = fill = 1.0
            left, top, width, height = map(int, stats[label, :4])
            component = cv2.compare(labels[top:top + height, left:left + width], int(label), cv2.CMP_EQ)
            contours, _ = cv2.findContours(component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE,
                                           offset=(left, top))
            if contours:
                contour = max(contours, key=cv2.contourArea)
                if len(contour) >= 5:
                    (ex, ey), (a, b), angle = cv2.fitEllipse(contour)
                    if math.isfinite(ex + ey + a + b + angle) and min(a, b) > 0:
                        ellipse = ((self.native_pixel(ex, scale), self.native_pixel(ey, scale)),
                                   (a / scale, b / scale), angle)
                        aspect = max(a, b) / min(a, b)
                        fill = area / (math.pi * ellipse[1][0] * ellipse[1][1] / 4)
                        # Keep the ellipse's shape scores in both modes so the
                        # test changes the center estimate, not the shape ranking.
                        if not self.center_of_mass:
                            cx, cy = ex, ey
            cx, cy = self.native_pixel(cx, scale), self.native_pixel(cy, scale)
            candidates.append({
                'label': int(label), 'area': area, 'local_x': cx, 'local_y': cy,
                'x': roi_x + cx, 'y': roi_y + cy, 'ellipse': ellipse,
                'aspect': aspect, 'fill': fill,
            })
        return candidates

    def clean_pupil_mask(self, mask, scale):
        if self.cleanup_radius == 0:
            return mask
        cleaned = self.timed('cleanup', cv2.morphologyEx, mask, cv2.MORPH_OPEN,
                             disk(self.cleanup_radius * scale), borderType=cv2.BORDER_CONSTANT,
                             borderValue=0)
        self.pupil_evidence = cleaned
        return cleaned

    @staticmethod
    def native_pixel(position, scale):
        # OpenCV linear resize aligns pixel centres, including this half-pixel shift.
        return (float(position) + 0.5) / scale - 0.5


def recording_metadata(video):
    """Read frame IDs and brightness logs only for a standard recording video."""
    if video.name.lower() != 'video.mkv':
        return [], {}
    frames, brightness = [], {}
    path = video.with_name('frames.csv')
    if path.is_file():
        with path.open(encoding='utf-8-sig', newline='') as stream:
            frames = [int(row['source_frame']) for row in csv.DictReader(stream)]
    path = video.with_name('tracking.csv')
    if frames and path.is_file():
        with path.open(encoding='utf-8-sig', newline='') as stream:
            for row in csv.DictReader(stream):
                if row.get('image_brightness'):
                    value = float(row['image_brightness'])
                    if 0.1 <= value <= 4:
                        brightness[int(row['source_frame'])] = value
    return frames, brightness


FIELDS = {
    'Pupil': ('pupil_thr', 'pupil_min', 'pupil_max'),
    'Corneal reflection': ('cr_thr', 'cr_min', 'cr_max'),
    'Tracking stability': ('pupil_gate', 'cr_gate', 'max_pair_dist', 'max_pair_vec_change',
                           'reacquire_after_frames'),
    'Template': ('template_tracking', 'template_radius', 'template_search_size', 'template_min_corr'),
    'Edge and adaptive methods': ('pupil_rays', 'pupil_edge_contrast', 'pupil_edge_threshold',
                                  'pupil_fit_error', 'pupil_adaptive_window', 'pupil_adaptive_offset'),
}
TITLES = {
    'pupil_thr': 'Dark threshold', 'pupil_min': 'Minimum area', 'pupil_max': 'Maximum area',
    'cr_thr': 'Bright threshold', 'cr_min': 'Minimum area', 'cr_max': 'Maximum area',
    'template_tracking': 'Follow template', 'image_brightness': 'Image brightness',
}


class ComparisonEyeView(EyeView):
    def __init__(self):
        super().__init__(crop=True)
        self.show_ellipse = False

    def paintEvent(self, event):
        super().paintEvent(event)
        pupil = self.result.get('pupil')
        ellipse = pupil.get('ellipse') if pupil else None
        if not self.show_ellipse or self.image is None or ellipse is None:
            return
        (x, y), (width, height), angle = ellipse
        if not all(map(math.isfinite, (x, y, width, height, angle))) or min(width, height) <= 0:
            return
        # MX Eye stores the fitted ellipse in crop coordinates, in native pixels.
        center = self._point((x + self.origin[0], y + self.origin[1]))
        iw, ih = self.view_geometry()
        with G.QPainter(self) as painter:
            painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
            painter.setClipRect(C.QRectF(*self.offset, iw, ih))
            painter.translate(center)
            painter.rotate(angle)
            painter.setPen(G.QPen(G.QColor('#ffdf71'), 1.6))
            painter.drawEllipse(C.QPointF(), width * self.display_scale / 2,
                                height * self.display_scale / 2)


class TracePlot(pg.PlotWidget):
    reset_requested = C.Signal()

    def mouseDoubleClickEvent(self, event):
        if event.button() == C.Qt.LeftButton:
            self.reset_requested.emit()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)


class Side(W.QWidget):
    changed = C.Signal()
    tool = C.Signal(str)

    def __init__(self, name, centroid):
        super().__init__()
        self.name = name
        self.settings = TrackingConfig()
        self.core = CompareTracker(self.settings)
        self.start_roi = tuple(self.core.roi)
        self.start_template_center = None
        self.result = None
        self.stats = OrderedDict()
        self.fields = {}
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        header = W.QHBoxLayout()
        tracker_label = W.QLabel(f'Tracker {name}')
        tracker_label.setStyleSheet(f'color: {COLORS[name]};')
        header.addWidget(tracker_label)
        self.fields['pupil_method'] = self.choice(
            header, None, [(title, method) for method, title in PUPIL_METHODS.items()])
        self.fields['pupil_method'].setToolTip('Pupil detection method for this tracker')
        ellipse_overlay = W.QCheckBox('Show ellipse')
        ellipse_overlay.setToolTip('Draw the actual pupil fit when the detector returns one.')
        ellipse_overlay.toggled.connect(self.toggle_ellipse)
        header.addWidget(ellipse_overlay)
        layout.addLayout(header)
        self.view = ComparisonEyeView()
        layout.addWidget(self.view, 2)
        self.status = W.QLabel('Open a video to begin')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        scroll = W.QScrollArea(widgetResizable=True)
        settings_widget = W.QWidget()
        controls = W.QVBoxLayout(settings_widget)
        form = W.QFormLayout()
        controls.addLayout(form)
        self.center = self.choice(form, 'Pupil center', [('Ellipse fit', False), ('Center of mass', True)])
        self.center.setCurrentIndex(int(centroid))
        self.sampling = self.choice(form, 'Pupil sampling', [('1× native', 1), ('2× bilinear', 2)])
        self.blur = W.QCheckBox('Light blur before threshold')
        form.addRow(self.blur)
        self.blur.toggled.connect(self.changed)
        self.blur_sigma = W.QDoubleSpinBox()
        self.blur_sigma.setRange(.1, 3.)
        self.blur_sigma.setSingleStep(.1)
        self.blur_sigma.setDecimals(2)
        self.blur_sigma.setValue(.6)
        self.blur_sigma.setSuffix(' px')
        self.blur_sigma.setKeyboardTracking(False)
        self.blur_sigma.setToolTip('Gaussian sigma in original camera pixels, before upsampling.')
        self.blur_sigma.valueChanged.connect(self.changed)
        form.addRow('Blur strength (σ)', self.blur_sigma)
        self.cleanup = Parameter('Mask cleanup', 0, 0, 20)
        self.cleanup.spin.setSuffix(' px')
        self.cleanup.setToolTip('Disk radius in original camera pixels. Erode then dilate the pupil mask. 0 turns cleanup off.')
        self.cleanup.changed.connect(self.changed)
        form.addRow(self.cleanup)
        self.recorded = W.QCheckBox('Use recorded brightness when available')
        self.recorded.setChecked(True)
        self.recorded.toggled.connect(self.changed)
        form.addRow(self.recorded)
        self.add_field(form, 'image_brightness')
        for key, title, choices in (
            ('tracking_mode', 'Tracking mode', [(v.value, v) for v in TrackingMode]),
            ('pupil_coordinates', 'Pupil output', [('Absolute', PupilCoordinates.ABSOLUTE),
                                                 ('ROI relative', PupilCoordinates.RELATIVE)]),
        ):
            self.fields[key] = self.choice(form, title, choices)
        for title, keys in FIELDS.items():
            section = Section(title, opened=title == 'Pupil')
            fields = W.QFormLayout()
            for key in keys:
                self.add_field(fields, key)
            section.body.addLayout(fields)
            if title == 'Template':
                button = W.QPushButton('Clear template')
                button.clicked.connect(lambda: self.tool.emit('clear_template'))
                section.body.addWidget(button)
            controls.addWidget(section)
        controls.addStretch()
        scroll.setWidget(settings_widget)
        layout.addWidget(scroll, 3)
        self.write_settings()

    def toggle_ellipse(self, checked):
        self.view.show_ellipse = checked
        self.view.update()

    def choice(self, layout, title, entries):
        box = W.QComboBox()
        for label, value in entries:
            box.addItem(label, value)
        if title is None:
            layout.addWidget(box, 1)
        else:
            layout.addRow(title, box)
        box.currentIndexChanged.connect(self.changed)
        return box

    def add_field(self, form, key):
        field = TrackingConfig.model_fields[key]
        kind = field.annotation
        title = TITLES.get(key, key.replace('_', ' ').capitalize())
        if kind is bool:
            box = W.QCheckBox(title)
            form.addRow(box)
            box.toggled.connect(self.changed)
        else:
            box = W.QSpinBox() if kind is int else W.QDoubleSpinBox()
            limits = {name: value for constraint in field.metadata for name in ('ge', 'gt', 'le')
                      if (value := getattr(constraint, name, None)) is not None}
            lower = limits.get('ge', (limits['gt'] + (1 if kind is int else .01)) if 'gt' in limits else 0)
            if kind is float:
                box.setDecimals(3)
                box.setSingleStep(.1 if key == 'image_brightness' else .05)
            box.setRange(lower, limits.get('le', 1_000_000))
            box.setKeyboardTracking(False)
            form.addRow(title, box)
            box.valueChanged.connect(self.changed)
        self.fields[key] = box
        box.setToolTip(TIPS.get(key, title))

    def write_settings(self):
        for key, box in self.fields.items():
            value = getattr(self.settings, key)
            with C.QSignalBlocker(box):
                if isinstance(box, W.QComboBox):
                    box.setCurrentIndex(box.findData(value))
                elif isinstance(box, W.QCheckBox):
                    box.setChecked(value)
                else:
                    box.setValue(value)
        self.update_experimental_controls()

    def update_experimental_controls(self):
        method = self.settings.pupil_method
        self.center.setEnabled(method in (PupilMethod.THRESHOLD, PupilMethod.ADAPTIVE))
        self.sampling.setEnabled(method is PupilMethod.THRESHOLD)
        self.blur.setEnabled(method is PupilMethod.THRESHOLD)
        self.blur_sigma.setEnabled(method is PupilMethod.THRESHOLD and self.blur.isChecked())
        self.cleanup.setEnabled(method in (PupilMethod.THRESHOLD, PupilMethod.ADAPTIVE))

    def read_settings(self):
        values = self.settings.model_dump()
        for key, box in self.fields.items():
            if isinstance(box, W.QComboBox):
                values[key] = box.currentData()
            elif isinstance(box, W.QCheckBox):
                values[key] = box.isChecked()
            else:
                values[key] = box.value()
        self.settings = TrackingConfig.model_validate(values)
        self.core.config = self.settings.model_copy(deep=True)
        self.core.sampling = int(self.sampling.currentData())
        self.core.blur = self.blur.isChecked()
        self.core.blur_sigma = self.blur_sigma.value()
        self.core.cleanup_radius = int(self.cleanup.spin.value())
        self.update_experimental_controls()
        self.core.center_of_mass = bool(self.center.currentData() and self.center.isEnabled())
        self.core.clear_feature_history()

    def configure(self, settings, template):
        self.settings = settings.model_copy(deep=True)
        self.core = CompareTracker(self.settings)
        if template is not None:
            restore_template(self.core, template)
        self.remember_roi()
        self.write_settings()
        self.read_settings()

    def remember_roi(self):
        self.start_roi = tuple(self.core.roi)
        self.start_template_center = self.core.template_last_center

    def seek(self):
        self.core.roi = list(self.start_roi)
        self.core.template_last_center = self.start_template_center
        self.core.clear_feature_history()

    def process(self, frame, index, source_id, media, logged_brightness, advance, delta):
        core = self.core
        core.config.image_brightness = (logged_brightness if logged_brightness is not None
                                        and self.recorded.isChecked() else self.settings.image_brightness)
        start = time.perf_counter_ns()
        self.result = core.process(frame, source_id, advance=advance, frame_delta=delta)
        elapsed = time.perf_counter_ns() - start
        if advance and index not in self.stats:
            result = self.result
            pupil, cr = result['pupil'], result['cr']
            self.stats[index] = (media / 1e9, core.timing_sample(elapsed), result['valid'],
                                 (pupil['x'], pupil['y']) if pupil else (math.nan, math.nan),
                                 (cr['x'], cr['y']) if cr else (math.nan, math.nan),
                                 (result['x'], result['y']))
            if len(self.stats) > TRACE_LIMIT:
                self.stats.popitem(last=False)

    def render(self):
        core = self.core
        x, y, w, h = (round(value) for value in self.result['roi'])
        crop = core.current_frame[y:y + h, x:x + w]
        evidence = core.pupil_evidence
        scale = 1
        if evidence is not None and evidence.shape == (crop.shape[0] * 2, crop.shape[1] * 2):
            scale = 2
            # Render the fine mask at full resolution. The display enlargement
            # uses nearest neighbor so it cannot invent another mask boundary.
            crop = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)
        self.view.set_frame(crop, self.result, scale=scale, origin=(x, y), params=core.config,
                            pupil_mask=True, cr_mask=True, pupil_evidence=evidence, inset=False)
        pupil, cr = self.result['pupil'], self.result['cr']
        report = []
        for name, blob in [('Pupil', pupil), ('CR', cr)]:
            report.append(f"{name} {blob['x']:.2f}, {blob['y']:.2f}" if blob else f'{name} —')
        report.append(f'{core.config.image_brightness:g}× brightness')
        first = '  ·  '.join(report)
        detail = self.result['reject_reason'] or core.template_status or self.result['track_status']
        self.status.setText(first + ('\n' + detail if detail else ''))


class Window(W.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('MX Eye · Offline tracker comparison')
        self.resize(1450, 920)
        self.reader = None
        self.frame = None
        self.index = -1
        self.media = 0
        self.frame_ids = []
        self.brightness = {}
        self.config_brightness = 1.0
        self.pending_frame = None
        self.play_started = None
        self.plot_updated = 0.
        self.timing_dialog = None
        self.timer = C.QTimer(self, interval=5)
        self.timer.timeout.connect(self.tick)

        main = W.QWidget()
        self.setCentralWidget(main)
        root = W.QVBoxLayout(main)
        top = W.QHBoxLayout()
        root.addLayout(top)
        self.button(top, 'Open video…', self.choose_video)
        self.button(top, 'Load MX Eye config for both…', self.choose_config)
        for src, dst in ((0, 1), (1, 0)):
            copy = self.button(top, f'Copy {"AB"[src]} → {"AB"[dst]}',
                               lambda checked=False, src=src, dst=dst: self.copy_settings(src, dst))
            copy.setToolTip('Copy tracking settings, ROI and template. Keep the destination’s pupil method and center estimator.')
        self.file_label = W.QLabel('Choose a video or its recording folder')
        self.file_label.setSizePolicy(W.QSizePolicy.Ignored, W.QSizePolicy.Preferred)
        top.addWidget(self.file_label, 1)

        views = W.QSplitter(C.Qt.Horizontal)
        source_panel = W.QWidget()
        source_layout = W.QVBoxLayout(source_panel)
        self.source_title = W.QLabel('Source video · ROI and template from A')
        source_layout.addWidget(self.source_title)
        self.source = EyeView()
        self.source.action.connect(lambda command, args: self.action(self.sides, command, args))
        source_layout.addWidget(self.source, 1)
        hint = W.QLabel('Drag to move the ROI, drag its corner to resize, or Shift-drag to draw it. '
                        'Right-click sets a template for both. In either eye view, left-click picks '
                        'the pupil, right-click picks the CR, and Shift-click sets its template.')
        hint.setWordWrap(True)
        source_layout.addWidget(hint)
        views.addWidget(source_panel)
        self.sides = [Side('A', True), Side('B', False)]
        for side in self.sides:
            views.addWidget(side)
            side.changed.connect(lambda side=side: self.settings_changed(side))
            side.view.action.connect(lambda command, args, side=side: self.action([side], command, args))
            side.tool.connect(lambda command, side=side: self.action([side], command, {}))
        views.setSizes([520, 400, 400])
        root.addWidget(views, 1)

        transport = W.QHBoxLayout()
        root.addLayout(transport)
        self.play = self.button(transport, 'Play', self.toggle_play)
        self.prev = self.button(transport, '−1 frame', lambda: self.seek(self.index - 1))
        self.next = self.button(transport, '+1 frame', lambda: self.seek(self.index + 1))
        self.prev.setToolTip('Previous frame (Left Arrow)')
        self.next.setToolTip('Next frame (Right Arrow)')
        self.slider = SeekSlider()
        self.slider.setRange(0, 0)
        self.slider.seek.connect(self.seek)
        transport.addWidget(self.slider, 1)
        self.frame_label = W.QLabel('Frame —')
        transport.addWidget(self.frame_label)
        for widget in (self.play, self.prev, self.next, self.slider):
            widget.setEnabled(False)

        row = W.QHBoxLayout()
        root.addLayout(row)
        self.signal = W.QComboBox()
        self.signal.addItems(['Pupil center', 'Corneal reflection', 'Tracker output'])
        self.signal.setCurrentText('Tracker output')
        self.signal.currentIndexChanged.connect(self.render_stats)
        row.addWidget(self.signal)
        row.addWidget(W.QLabel(f'Up to {TRACE_LIMIT} measured frames'))
        self.button(row, 'Timing details…', self.show_timings)
        self.metrics = W.QLabel('Play or step to collect timings')
        row.addWidget(self.metrics, 1)
        plots = W.QHBoxLayout()
        root.addLayout(plots)
        self.lines = []
        self.plots = []
        for axis in ('X', 'Y'):
            plot = TracePlot(background='#151a20')
            plot.setToolTip('Double-click to reset both plots to automatic scaling.')
            plot.reset_requested.connect(self.reset_plot_view)
            plot.setMinimumHeight(155)
            plot.setMaximumHeight(190)
            plot.setLabel('left', axis, units='px')
            plot.setLabel('bottom', 'Video time', units='s')
            plot.showGrid(x=True, y=True, alpha=.2)
            plots.addWidget(plot, 1)
            self.plots.append(plot)
            self.lines.append([plot.plot(pen=pg.mkPen(color, width=2), symbol='o',
                                         symbolSize=3, symbolPen=None, symbolBrush=pg.mkBrush(color))
                               for color in COLORS.values()])
        self.plots[1].setXLink(self.plots[0])
        W.QApplication.instance().installEventFilter(self)

    def reset_plot_view(self):
        self.plots[0].getViewBox().enableAutoRange(x=True, y=True)
        self.plots[1].getViewBox().enableAutoRange(y=True)

    def show_timings(self):
        if self.timing_dialog is None:
            self.timing_dialog = W.QDialog(self)
            self.timing_dialog.setWindowTitle('A/B processing time')
            self.timing_dialog.resize(720, 450)
            layout = W.QVBoxLayout(self.timing_dialog)
            self.timing_context = W.QLabel()
            self.timing_context.setWordWrap(True)
            layout.addWidget(self.timing_context)
            self.timing_table = W.QTableWidget(len(TIMING_STAGES), 5)
            self.timing_table.setHorizontalHeaderLabels([
                'Processing step', 'A median / p95', 'B median / p95', 'B − A median', 'Runs A / B',
            ])
            self.timing_table.verticalHeader().hide()
            self.timing_table.setEditTriggers(W.QAbstractItemView.EditTrigger.NoEditTriggers)
            self.timing_table.horizontalHeader().setSectionResizeMode(W.QHeaderView.ResizeMode.ResizeToContents)
            self.timing_table.horizontalHeader().setStretchLastSection(True)
            for row, (_, title) in enumerate(TIMING_STAGES):
                self.timing_table.setItem(row, 0, W.QTableWidgetItem(title))
                for column in range(1, 5):
                    self.timing_table.setItem(row, column, W.QTableWidgetItem())
            layout.addWidget(self.timing_table)
            note = W.QLabel('Times are milliseconds per frame. Δ is the median of the per-frame differences. '
                            'Other detection includes native thresholding, edge-based detection and tracking gates. '
                            'Decoding and drawing are outside the timer.')
            note.setWordWrap(True)
            layout.addWidget(note)
        self.update_timings()
        self.timing_dialog.show()
        self.timing_dialog.raise_()

    @staticmethod
    def timing_description(side):
        core = side.core
        fields = [f'{side.name} · {side.fields["pupil_method"].currentText()}']
        method = core.config.pupil_method
        if method in (PupilMethod.THRESHOLD, PupilMethod.ADAPTIVE):
            fields.append(side.center.currentText())
            fields.append(f'cleanup {core.cleanup_radius} px')
        if method is PupilMethod.THRESHOLD:
            fields.append(f'{core.sampling}× sampling')
            fields.append(f'blur σ {core.blur_sigma:g} px' if core.blur else 'blur off')
        return ' · '.join(fields)

    def update_timings(self):
        a, b = self.sides
        common = sorted(a.stats.keys() & b.stats.keys())
        descriptions = [self.timing_description(side) for side in self.sides]
        self.timing_context.setText('\n'.join([*descriptions, f'{len(common)} paired frames']))
        samples = [np.asarray([s.stats[index][1] for index in common]) for s in self.sides]
        for row in range(len(TIMING_STAGES)):
            columns = ['—', '—', '—', '0 / 0']
            if common:
                av, bv = (sample[:, row] for sample in samples)
                active_a, active_b = np.count_nonzero(av), np.count_nonzero(bv)
                for column, series in enumerate((av, bv)):
                    if np.any(series):
                        median, p95 = np.percentile(series, [50, 95])
                        columns[column] = f'{median:.3f} / {p95:.3f}'
                if active_a or active_b:
                    columns[2] = f'{np.median(bv - av):+.3f}'
                columns[3] = f'{active_a} / {active_b}'
            for column, value in enumerate(columns, 1):
                self.timing_table.item(row, column).setText(value)

    def eventFilter(self, watched, event):
        if (event.type() == C.QEvent.Type.KeyPress and self.reader is not None
                and event.key() in (C.Qt.Key_Left, C.Qt.Key_Right)
                and event.modifiers() == C.Qt.NoModifier
                and isinstance(watched, W.QWidget) and watched.window() is self):
            focus = self.focusWidget()
            editing = isinstance(focus, (W.QLineEdit, W.QAbstractSpinBox, W.QComboBox))
            editing |= isinstance(focus, W.QSlider) and focus is not self.slider
            if not editing:
                self.seek(self.index + (-1 if event.key() == C.Qt.Key_Left else 1))
                return True
        return super().eventFilter(watched, event)

    @staticmethod
    def button(layout, title, action):
        button = W.QPushButton(title)
        button.clicked.connect(action)
        layout.addWidget(button)
        return button

    def warn(self, title, exc):
        self.pause()
        W.QMessageBox.warning(self, title, str(exc))

    def choose_video(self):
        path, _ = W.QFileDialog.getOpenFileName(self, 'Open video', '',
                                                'Video (*.mkv *.mp4 *.avi *.mov *.m4v *.webm);;All files (*)')
        if path:
            self.load_video(Path(path))

    def load_video(self, path):
        self.pause()
        path = path / 'video.mkv' if path.is_dir() else path
        new_reader = None
        try:
            new_reader = VideoReader(str(path), Event())
            first = new_reader.read(0)
            if first is None:
                raise ValueError('The video has no decodable frames.')
            frames, brightness = recording_metadata(path)
            config_path = path.with_name('config.json')
            config = (MxEyeConfigStore.load(config_path).value if path.name.lower() == 'video.mkv'
                      and config_path.is_file() else None)
            if config is not None and config.template is not None:
                restore_template(Tracker(config.tracking), config.template)
        except (OSError, ValueError, TypeError, KeyError, cv2.error) as exc:
            if new_reader is not None:
                new_reader.close()
            self.warn('Cannot open video or recording', exc)
            return False
        if self.reader is not None:
            self.reader.close()
        self.reader = new_reader
        self.frame_ids, self.brightness = frames, brightness
        self.index = -1
        settings = config.tracking if config is not None else TrackingConfig()
        template = config.template if config is not None else None
        self.configure_both(settings, template)
        self.file_label.setText(f"{path.name}  ·  {'Recording settings' if config is not None else 'MX Eye defaults'}")
        self.file_label.setToolTip(str(path))
        self.source.reset_zoom()
        for widget in (self.play, self.prev, self.next, self.slider):
            widget.setEnabled(True)
        self.show_frame(first)
        return True

    def choose_config(self):
        self.pause()
        path, _ = W.QFileDialog.getOpenFileName(self, 'Load MX Eye configuration', '', 'JSON (*.json)')
        if not path:
            return
        try:
            config = MxEyeConfigStore.load(Path(path)).value
            if config.template is not None:
                restore_template(Tracker(config.tracking), config.template)
            self.configure_both(config.tracking, config.template)
            if self.frame is not None:
                self.evaluate()
            self.file_label.setText(f'Loaded tracking settings and template from {Path(path).name}')
        except (OSError, ValueError, TypeError, cv2.error) as exc:
            self.warn('Cannot load configuration', exc)

    def configure_both(self, settings, template):
        self.config_brightness = settings.image_brightness
        for side in self.sides:
            side.configure(settings, template)
        self.clear_stats()

    def copy_settings(self, source_index, target_index):
        self.pause()
        source, target = self.sides[source_index], self.sides[target_index]
        settings = source.settings.model_copy(update={
            'pupil_method': target.settings.pupil_method,
            'roi': tuple(source.core.roi),
        })
        template = serialize_template(source.core)
        if template is not None:
            template = TemplateConfig.model_validate(template)
        with (C.QSignalBlocker(target.sampling), C.QSignalBlocker(target.blur),
              C.QSignalBlocker(target.blur_sigma), C.QSignalBlocker(target.recorded),
              C.QSignalBlocker(target.cleanup)):
            target.sampling.setCurrentIndex(target.sampling.findData(source.core.sampling))
            target.blur.setChecked(source.core.blur)
            target.blur_sigma.setValue(source.core.blur_sigma)
            target.recorded.setChecked(source.recorded.isChecked())
            target.cleanup.spin.setValue(source.core.cleanup_radius)
        target.configure(settings, template)
        self.clear_stats()
        if self.frame is not None:
            self.evaluate()

    def source_id(self, index):
        return self.frame_ids[index] if 0 <= index < len(self.frame_ids) else index + 1

    def evaluate(self, advance=False, delta=1):
        source_id = self.source_id(self.index)
        recorded = self.brightness.get(source_id)
        # Alternate who goes first so neither tracker always gets a warm cache.
        order = reversed(self.sides) if self.index % 2 else self.sides
        for side in order:
            side.process(self.frame, self.index, source_id, self.media, recorded, advance, delta)
        for side in self.sides:
            side.render()
        a = self.sides[0]
        brightness = self.config_brightness if recorded is None else recorded
        main_frame = self.frame if brightness == 1.0 else cv2.convertScaleAbs(self.frame, alpha=brightness)
        source = 'config' if recorded is None else 'recorded'
        self.source_title.setText(f'Source video · {brightness:g}× ({source}) · ROI from A')
        self.source.set_frame(main_frame, a.result, params=a.core.config, template=a.core.template,
                              crosshairs=False)
        if not self.timer.isActive() or time.monotonic() - self.plot_updated > .1:
            self.render_stats()

    def show_frame(self, entry):
        frame, index, media = entry
        previous = self.index
        if previous >= 0 and index != previous + 1 and index != previous:
            for side in self.sides:
                side.seek()
        delta = max(1, self.source_id(index) - self.source_id(previous)) if index == previous + 1 else 1
        self.frame, self.index, self.media = frame, index, media
        self.evaluate(advance=index != previous, delta=delta)
        known = self.reader.total or max(self.reader.estimated_total, len(self.frame_ids), index + 2)
        with C.QSignalBlocker(self.slider):
            self.slider.setRange(0, max(0, known - 1))
            self.slider.setValue(index)
        self.frame_label.setText(f'Frame {index + 1} · source {self.source_id(index)} · {media / 1e9:.3f} s')

    def clear_stats(self):
        for side in self.sides:
            side.stats.clear()
        self.render_stats()

    def render_stats(self):
        self.plot_updated = time.monotonic()
        column = 3 + self.signal.currentIndex()
        report = []
        for i, side in enumerate(self.sides):
            frames = sorted(side.stats)
            rows = [side.stats[frame] for frame in frames]
            times = [row[0] for row in rows]
            connected = np.diff(frames, append=-1) == 1
            for axis in range(2):
                values = np.asarray([row[column][axis] for row in rows])
                finite = np.isfinite(values)
                connect = connected & finite & np.roll(finite, -1)
                self.lines[axis][i].setData(times, values, connect=connect)
            if rows:
                median, p95 = np.percentile([row[1][0] for row in rows], [50, 95])
                missed = sum(not row[2] for row in rows)
                report.append(f'{side.name}  {median:.2f}/{p95:.2f} ms  ·  missed {missed}/{len(rows)}')
        self.metrics.setText('    |    '.join(report) + '  (median/p95)' if report else 'Play or step to collect timings')
        if self.timing_dialog is not None and self.timing_dialog.isVisible():
            self.update_timings()

    def settings_changed(self, side):
        try:
            side.read_settings()
        except ValueError:
            side.status.setText('Minimum area must be at or below maximum area.')
            return
        self.clear_stats()
        if self.frame is not None:
            self.evaluate()

    def action(self, sides, command, args):
        if self.frame is None:
            return
        self.pause()
        for side in sides:
            core = side.core
            if command == 'roi':
                core.roi = list(args['roi'])
                core.clamp_roi()
                core.clear_feature_history()
                side.remember_roi()
            elif command == 'search':
                core.template_anchor = tuple(args['center'])
                core.config.template_search_size = round(args['size'])
            elif command == 'template':
                core.set_template(*args['point'])
                side.remember_roi()
            elif command == 'pick':
                core.pick_feature(args['kind'], *args['point'])
            elif command == 'clear_template':
                core.clear_template()
                side.remember_roi()
            side.settings = core.config.model_copy(update={'image_brightness': side.settings.image_brightness})
            side.write_settings()
        self.clear_stats()
        self.evaluate()

    def pause(self):
        self.timer.stop()
        self.play.setText('Play')
        self.pending_frame = None
        self.play_started = None

    def seek(self, target):
        if self.reader is None:
            return
        self.pause()
        try:
            entry = self.reader.read(max(0, target))
            if entry is not None:
                self.show_frame(entry)
        except (OSError, ValueError, cv2.error) as exc:
            self.warn('Cannot read frame', exc)

    def toggle_play(self):
        if self.timer.isActive():
            self.pause()
        elif self.reader is not None:
            if self.reader.total is not None and self.index >= self.reader.total - 1:
                self.seek(0)
            self.play.setText('Pause')
            self.timer.start()

    def tick(self):
        try:
            if self.pending_frame is None:
                self.pending_frame = self.reader.read(self.index + 1)
            if self.pending_frame is None or self.pending_frame[1] <= self.index:
                self.pause()
                self.slider.setMaximum(max(0, self.index))
                self.render_stats()
                return
            now = time.monotonic()
            if self.play_started is None:
                self.play_started = now - self.media / 1e9
            if now >= self.play_started + self.pending_frame[2] / 1e9:
                frame, self.pending_frame = self.pending_frame, None
                self.show_frame(frame)
        except (OSError, ValueError, cv2.error) as exc:
            self.warn('Cannot play video', exc)

    def closeEvent(self, event):
        self.pause()
        if self.reader is not None:
            self.reader.close()
        super().closeEvent(event)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video', nargs='?', type=Path, help='Video file or MX Eye recording folder')
    args = parser.parse_args()
    cv2.setNumThreads(1)  # Same setting as the live tracking worker.
    app = W.QApplication(sys.argv[:1])
    app.setStyle('Fusion')
    app.setStyleSheet(STYLE)
    window = Window()
    window.show()
    if args.video is not None:
        C.QTimer.singleShot(0, lambda: window.load_video(args.video))
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
