#!/usr/bin/env python3

import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal, QRectF, QPointF
from PyQt5.QtGui import QImage, QPixmap, QPainter, QPen, QColor
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


APP_DIR = Path.home() / ".mxbi_camera_monitor"
SETTINGS_FILE = APP_DIR / "settings.json"
LOG_FILE = APP_DIR / "recordings.jsonl"
SHM_ROOT = Path("/dev/shm/mxbi_camera_monitor")


DEFAULTS = {
    "device": "/dev/video0",
    "camera_id": "",
    "resolution": "1280x720",
    "record_fps": 60,
    "preview_fps": 5,
    "motion_threshold_percent": 4,
    "pixel_diff_threshold": 25,
    "stop_after_seconds": 2.0,
    "pretrigger_seconds": 2.0,
    "min_motion_frames": 3,
    "save_dir": str(Path.home() / "mxbi_recordings"),
    "detection_area": "Full frame",
    "roi_norm": [0.20, 0.20, 0.60, 0.60],
}



class ROIImageLabel(QLabel):
    roi_changed = pyqtSignal(object)

    def __init__(self, text="", editable=False):
        super().__init__(text)
        self.editable = editable
        self.roi_enabled = False
        self.roi_norm = [0.20, 0.20, 0.60, 0.60]  # x, y, w, h in image coordinates

        self._drag_mode = None
        self._drag_start = None
        self._start_roi = None
        self._handle_px = 10

        self.setMouseTracking(True)

    def set_roi(self, roi_norm):
        x, y, w, h = [float(v) for v in roi_norm]
        self.roi_norm = self._clamp_roi([x, y, w, h])
        self.update()

    def set_roi_enabled(self, enabled):
        self.roi_enabled = bool(enabled)
        self.update()

    def reset_roi(self):
        self.set_roi([0.20, 0.20, 0.60, 0.60])
        self.roi_changed.emit(self.roi_norm.copy())

    def _pixmap_rect(self):
        pix = self.pixmap()
        if pix is None or pix.isNull():
            return QRectF()

        pw = pix.width()
        ph = pix.height()
        if pw <= 0 or ph <= 0:
            return QRectF()

        scale = min(self.width() / pw, self.height() / ph)
        dw = pw * scale
        dh = ph * scale
        x0 = (self.width() - dw) / 2.0
        y0 = (self.height() - dh) / 2.0
        return QRectF(x0, y0, dw, dh)

    def _roi_rect_widget(self):
        r = self._pixmap_rect()
        if r.isNull():
            return QRectF()

        x, y, w, h = self.roi_norm
        return QRectF(
            r.left() + x * r.width(),
            r.top() + y * r.height(),
            w * r.width(),
            h * r.height(),
        )

    def _widget_to_norm(self, pos):
        r = self._pixmap_rect()
        if r.isNull():
            return None

        x = (pos.x() - r.left()) / r.width()
        y = (pos.y() - r.top()) / r.height()
        return QPointF(
            max(0.0, min(1.0, x)),
            max(0.0, min(1.0, y)),
        )

    def _clamp_roi(self, roi):
        x, y, w, h = roi
        min_size = 0.03

        w = max(min_size, min(1.0, w))
        h = max(min_size, min(1.0, h))
        x = max(0.0, min(1.0 - w, x))
        y = max(0.0, min(1.0 - h, y))

        return [x, y, w, h]

    def _hit_test(self, pos):
        rr = self._roi_rect_widget()
        if rr.isNull():
            return None

        px = pos.x()
        py = pos.y()
        h = self._handle_px

        near_left = abs(px - rr.left()) <= h
        near_right = abs(px - rr.right()) <= h
        near_top = abs(py - rr.top()) <= h
        near_bottom = abs(py - rr.bottom()) <= h

        within_x = rr.left() - h <= px <= rr.right() + h
        within_y = rr.top() - h <= py <= rr.bottom() + h

        if near_left and near_top:
            return "tl"
        if near_right and near_top:
            return "tr"
        if near_left and near_bottom:
            return "bl"
        if near_right and near_bottom:
            return "br"
        if near_left and within_y:
            return "l"
        if near_right and within_y:
            return "r"
        if near_top and within_x:
            return "t"
        if near_bottom and within_x:
            return "b"
        if rr.contains(pos):
            return "move"

        return None

    def mousePressEvent(self, event):
        if (
            not self.editable
            or not self.roi_enabled
            or event.button() != Qt.LeftButton
        ):
            super().mousePressEvent(event)
            return

        mode = self._hit_test(event.pos())
        if mode is None:
            return

        p = self._widget_to_norm(event.pos())
        if p is None:
            return

        self._drag_mode = mode
        self._drag_start = p
        self._start_roi = self.roi_norm.copy()
        event.accept()

    def mouseMoveEvent(self, event):
        if not self.editable or not self.roi_enabled:
            super().mouseMoveEvent(event)
            return

        if self._drag_mode is None:
            mode = self._hit_test(event.pos())
            if mode == "move":
                self.setCursor(Qt.SizeAllCursor)
            elif mode in ("l", "r"):
                self.setCursor(Qt.SizeHorCursor)
            elif mode in ("t", "b"):
                self.setCursor(Qt.SizeVerCursor)
            elif mode in ("tl", "br"):
                self.setCursor(Qt.SizeFDiagCursor)
            elif mode in ("tr", "bl"):
                self.setCursor(Qt.SizeBDiagCursor)
            else:
                self.setCursor(Qt.ArrowCursor)
            return

        p = self._widget_to_norm(event.pos())
        if p is None:
            return

        sx, sy, sw, sh = self._start_roi
        dx = p.x() - self._drag_start.x()
        dy = p.y() - self._drag_start.y()

        left = sx
        top = sy
        right = sx + sw
        bottom = sy + sh

        mode = self._drag_mode

        if mode == "move":
            width = sw
            height = sh
            left = max(0.0, min(1.0 - width, sx + dx))
            top = max(0.0, min(1.0 - height, sy + dy))
            right = left + width
            bottom = top + height
        else:
            if "l" in mode:
                left = max(0.0, min(right - 0.03, sx + dx))
            if "r" in mode:
                right = min(1.0, max(left + 0.03, sx + sw + dx))
            if "t" in mode:
                top = max(0.0, min(bottom - 0.03, sy + dy))
            if "b" in mode:
                bottom = min(1.0, max(top + 0.03, sy + sh + dy))

        self.roi_norm = self._clamp_roi(
            [left, top, right - left, bottom - top]
        )
        self.update()
        self.roi_changed.emit(self.roi_norm.copy())
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._drag_mode is not None and event.button() == Qt.LeftButton:
            self._drag_mode = None
            self._drag_start = None
            self._start_roi = None
            self.roi_changed.emit(self.roi_norm.copy())
            event.accept()
            return

        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)

        if not self.roi_enabled:
            return

        rr = self._roi_rect_widget()
        if rr.isNull():
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Dim the frame outside the ROI.
        image_rect = self._pixmap_rect()
        shade = QColor(0, 0, 0, 95)
        painter.fillRect(QRectF(image_rect.left(), image_rect.top(), image_rect.width(), rr.top() - image_rect.top()), shade)
        painter.fillRect(QRectF(image_rect.left(), rr.bottom(), image_rect.width(), image_rect.bottom() - rr.bottom()), shade)
        painter.fillRect(QRectF(image_rect.left(), rr.top(), rr.left() - image_rect.left(), rr.height()), shade)
        painter.fillRect(QRectF(rr.right(), rr.top(), image_rect.right() - rr.right(), rr.height()), shade)

        pen = QPen(QColor(255, 215, 0))
        pen.setWidth(3)
        painter.setPen(pen)
        painter.drawRect(rr)

        handle = 8
        painter.setBrush(QColor(255, 215, 0))
        for x, y in [
            (rr.left(), rr.top()),
            (rr.right(), rr.top()),
            (rr.left(), rr.bottom()),
            (rr.right(), rr.bottom()),
        ]:
            painter.drawRect(
                QRectF(x - handle / 2, y - handle / 2, handle, handle)
            )

        painter.end()


class PreviewThread(QThread):
    frame_ready = pyqtSignal(object, object, float)
    stream_error = pyqtSignal(str)

    def __init__(
        self,
        pipe,
        preview_width,
        preview_height,
        sample_every_n_frames,
        pixel_diff_threshold,
    ):
        super().__init__()
        self.pipe = pipe
        self.preview_width = preview_width
        self.preview_height = preview_height
        self.sample_every_n_frames = max(1, int(sample_every_n_frames))
        self.pixel_diff_threshold = pixel_diff_threshold
        self.running = True

    def stop(self):
        self.running = False

    def run(self):
        # FFmpeg sends the camera's original MJPEG stream to this pipe without
        # decoding or re-encoding. We read every JPEG frame, but only decode
        # every Nth one for the GUI/motion detector.
        buffer = bytearray()
        camera_frame_index = 0
        previous = None

        while self.running:
            chunk = self.pipe.read(65536)

            if not chunk:
                if self.running:
                    self.stream_error.emit("FFmpeg preview pipe closed.")
                return

            buffer.extend(chunk)

            while self.running:
                soi = buffer.find(b"\xff\xd8")

                if soi < 0:
                    if len(buffer) > 1:
                        del buffer[:-1]
                    break

                eoi = buffer.find(b"\xff\xd9", soi + 2)

                if eoi < 0:
                    if soi > 0:
                        del buffer[:soi]
                    break

                jpeg = bytes(buffer[soi : eoi + 2])
                del buffer[: eoi + 2]

                camera_frame_index += 1

                if camera_frame_index % self.sample_every_n_frames != 0:
                    continue

                encoded = np.frombuffer(jpeg, dtype=np.uint8)
                full_frame = cv2.imdecode(encoded, cv2.IMREAD_COLOR)

                if full_frame is None:
                    continue

                frame = cv2.resize(
                    full_frame,
                    (self.preview_width, self.preview_height),
                    interpolation=cv2.INTER_AREA,
                )

                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                gray = cv2.GaussianBlur(gray, (7, 7), 0)

                if previous is None:
                    mask = np.zeros_like(gray)
                    changed_fraction = 0.0
                else:
                    diff = cv2.absdiff(gray, previous)
                    _, mask = cv2.threshold(
                        diff,
                        self.pixel_diff_threshold,
                        255,
                        cv2.THRESH_BINARY,
                    )

                    mask = cv2.morphologyEx(
                        mask,
                        cv2.MORPH_OPEN,
                        np.ones((3, 3), np.uint8),
                    )

                    changed_fraction = (
                        float(cv2.countNonZero(mask)) / float(mask.size)
                    )

                previous = gray

                self.frame_ready.emit(
                    frame.copy(),
                    mask.copy(),
                    changed_fraction,
                )

class MXBICameraMonitor(QMainWindow):
    def __init__(self):
        super().__init__()

        APP_DIR.mkdir(parents=True, exist_ok=True)
        SHM_ROOT.mkdir(parents=True, exist_ok=True)

        self.settings = DEFAULTS.copy()
        if SETTINGS_FILE.exists():
            try:
                self.settings.update(json.loads(SETTINGS_FILE.read_text()))
            except Exception:
                pass

        self.capture_process = None
        self.capture_stderr = None
        self.source_process = None
        self.source_stderr = None
        self.preview_thread = None
        self.run_dir = None

        # Filled by refresh_cameras(). Each entry contains a backend
        # ("v4l2" or "rpicam"), a human-readable name and the exact
        # resolution/FPS combinations reported by that camera.
        self.camera_sources = {}

        self.processed_segments = set()
        self.ring_segments = deque()
        self.segment_seconds = 1.0

        self.recording = False
        self.manual_recording = False
        self.stop_requested = False
        self.stop_target_segment = None
        self.event_dir = None
        self.event_start_wall = None
        self.event_start_monotonic = None
        self.event_output = None
        self.event_segment_count = 0

        self.motion_frames = 0
        self.last_motion_time = 0.0

        # Safety choice: automatic triggering always starts DISARMED when the
        # application launches. Preview and manual recording still work.
        self.armed = False

        self.setWindowTitle("MXBI Camera Monitor")
        self.resize(1450, 850)

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)

        left = QVBoxLayout()
        right = QVBoxLayout()
        root.addLayout(left, 4)
        root.addLayout(right, 2)

        views = QGridLayout()

        self.live_label = ROIImageLabel("Live camera", editable=True)
        self.live_label.setAlignment(Qt.AlignCenter)
        self.live_label.setMinimumSize(480, 270)
        self.live_label.setStyleSheet("background: #111; color: white;")

        self.diff_label = ROIImageLabel("Motion / difference", editable=False)
        self.diff_label.setAlignment(Qt.AlignCenter)
        self.diff_label.setMinimumSize(480, 270)
        self.diff_label.setStyleSheet("background: #111; color: white;")

        saved_roi = self.settings.get("roi_norm", [0.20, 0.20, 0.60, 0.60])
        self.live_label.set_roi(saved_roi)
        self.diff_label.set_roi(saved_roi)
        self.live_label.roi_changed.connect(self.on_roi_changed)

        views.addWidget(QLabel("<b>LIVE CAMERA</b>"), 0, 0)
        views.addWidget(QLabel("<b>MOTION / DIFFERENCE</b>"), 0, 1)
        views.addWidget(self.live_label, 1, 0)
        views.addWidget(self.diff_label, 1, 1)
        left.addLayout(views)

        motion_row = QHBoxLayout()

        self.motion_label = QLabel("Motion: 0.000 %")
        self.motion_bar = QProgressBar()
        self.motion_bar.setRange(0, 1000)
        self.motion_bar.setFormat("0–10% scale")

        motion_row.addWidget(self.motion_label)
        motion_row.addWidget(self.motion_bar, 1)
        left.addLayout(motion_row)

        state_row = QHBoxLayout()

        self.camera_state = QLabel("● CAMERA OFF")
        self.arm_state = QLabel("● DISARMED")
        self.record_state = QLabel("● NOT RECORDING")

        self.camera_state.setStyleSheet("font-weight: bold;")
        self.arm_state.setStyleSheet(
            "font-weight: bold; color: rgb(170, 70, 70);"
        )
        self.record_state.setStyleSheet("font-weight: bold;")

        state_row.addWidget(self.camera_state)
        state_row.addStretch()
        state_row.addWidget(self.arm_state)
        state_row.addStretch()
        state_row.addWidget(self.record_state)

        left.addLayout(state_row)

        controls_box = QGroupBox("Detection / recording settings")
        form = QFormLayout(controls_box)

        self.camera_combo = QComboBox()
        self.camera_combo.currentIndexChanged.connect(self.on_camera_changed)

        self.refresh_camera_button = QPushButton("Refresh")
        self.refresh_camera_button.clicked.connect(self.refresh_cameras)

        camera_row = QHBoxLayout()
        camera_row.addWidget(self.camera_combo, 1)
        camera_row.addWidget(self.refresh_camera_button)

        self.resolution_combo = QComboBox()
        self.resolution_combo.currentTextChanged.connect(
            self.on_resolution_changed
        )

        self.record_fps_combo = QComboBox()

        self.preview_fps_spin = QSpinBox()
        self.preview_fps_spin.setRange(1, 15)
        self.preview_fps_spin.setValue(int(self.settings["preview_fps"]))

        self.motion_threshold_spin = QDoubleSpinBox()
        self.motion_threshold_spin.setRange(0.01, 50.0)
        self.motion_threshold_spin.setDecimals(2)
        self.motion_threshold_spin.setSingleStep(0.1)
        self.motion_threshold_spin.setSuffix(" %")
        self.motion_threshold_spin.setValue(
            float(self.settings["motion_threshold_percent"])
        )

        self.pixel_threshold_spin = QSpinBox()
        self.pixel_threshold_spin.setRange(1, 100)
        self.pixel_threshold_spin.setValue(
            int(self.settings["pixel_diff_threshold"])
        )

        self.stop_after_spin = QDoubleSpinBox()
        self.stop_after_spin.setRange(0.5, 300.0)
        self.stop_after_spin.setDecimals(1)
        self.stop_after_spin.setSuffix(" s")
        self.stop_after_spin.setValue(
            float(self.settings["stop_after_seconds"])
        )

        self.pretrigger_spin = QDoubleSpinBox()
        self.pretrigger_spin.setRange(0.0, 20.0)
        self.pretrigger_spin.setDecimals(1)
        self.pretrigger_spin.setSuffix(" s")
        self.pretrigger_spin.setValue(
            float(self.settings["pretrigger_seconds"])
        )

        self.min_motion_frames_spin = QSpinBox()
        self.min_motion_frames_spin.setRange(1, 30)
        self.min_motion_frames_spin.setValue(
            int(self.settings["min_motion_frames"])
        )

        self.detection_area_combo = QComboBox()
        self.detection_area_combo.addItems(["Full frame", "ROI"])
        self.detection_area_combo.setCurrentText(
            self.settings.get("detection_area", "Full frame")
        )
        self.detection_area_combo.currentTextChanged.connect(
            self.on_detection_area_changed
        )

        self.reset_roi_button = QPushButton("Reset ROI")
        self.reset_roi_button.clicked.connect(self.live_label.reset_roi)

        detection_row = QHBoxLayout()
        detection_row.addWidget(self.detection_area_combo)
        detection_row.addWidget(self.reset_roi_button)

        save_row = QHBoxLayout()
        self.save_dir_edit = QLineEdit(self.settings["save_dir"])
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.choose_save_dir)
        save_row.addWidget(self.save_dir_edit, 1)
        save_row.addWidget(browse)

        form.addRow("Camera:", camera_row)
        form.addRow("Recording resolution:", self.resolution_combo)
        form.addRow("Recording FPS:", self.record_fps_combo)
        form.addRow("Preview / detection FPS:", self.preview_fps_spin)
        form.addRow("Motion trigger threshold:", self.motion_threshold_spin)
        form.addRow("Per-pixel difference threshold:", self.pixel_threshold_spin)
        form.addRow("Stop after no motion:", self.stop_after_spin)
        form.addRow("Pre-trigger buffer:", self.pretrigger_spin)
        form.addRow("Consecutive motion frames:", self.min_motion_frames_spin)
        form.addRow("Detection area:", detection_row)
        form.addRow("Save folder:", save_row)

        note = QLabel(
            "Camera modes are read from the selected camera, so only supported "
            "resolution/FPS combinations are offered. USB/V4L2 cameras use their "
            "native MJPEG stream. Raspberry Pi CSI cameras use rpicam-vid MJPEG. "
            "FFmpeg stream-copies the recording into MKV; only sampled frames are "
            "decoded for preview/motion. "
            "ARM enables automatic motion-triggered recording; DISARM leaves "
            "the live preview and manual recording available. Detection can use "
            "the full frame or an adjustable ROI drawn over the live image."
        )
        note.setWordWrap(True)
        form.addRow(note)

        left.addWidget(controls_box)
        self.on_detection_area_changed(self.detection_area_combo.currentText())

        button_row = QHBoxLayout()

        self.arm_button = QPushButton("ARM")
        self.arm_button.setMinimumHeight(42)
        self.arm_button.setStyleSheet(
            "font-weight: bold; font-size: 16px; "
            "background-color: rgb(210, 225, 210);"
        )

        self.apply_button = QPushButton("Apply / Restart Camera")
        self.manual_button = QPushButton("● Start Manual Recording")
        self.stop_button = QPushButton("■ Stop Recording")

        self.arm_button.clicked.connect(self.toggle_armed)
        self.apply_button.clicked.connect(self.apply_and_restart)
        self.manual_button.clicked.connect(self.start_manual_recording)
        self.stop_button.clicked.connect(self.request_stop_recording)

        button_row.addWidget(self.arm_button)
        button_row.addWidget(self.apply_button)
        button_row.addWidget(self.manual_button)
        button_row.addWidget(self.stop_button)

        left.addLayout(button_row)

        right.addWidget(QLabel("<h3>Recordings</h3>"))

        self.recording_count_label = QLabel("Videos recorded: 0")
        right.addWidget(self.recording_count_label)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Start", "Duration", "File", "Trigger"]
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        right.addWidget(self.table, 1)

        self.disk_label = QLabel("Disk: —")
        right.addWidget(self.disk_label)

        self.open_folder_button = QPushButton("Open recordings folder")
        self.open_folder_button.clicked.connect(self.open_recordings_folder)
        right.addWidget(self.open_folder_button)

        self.status_label = QLabel("Starting…")
        self.status_label.setWordWrap(True)
        right.addWidget(self.status_label)

        self.segment_timer = QTimer(self)
        self.segment_timer.timeout.connect(self.process_segments)
        self.segment_timer.start(250)

        self.housekeeping_timer = QTimer(self)
        self.housekeeping_timer.timeout.connect(self.update_housekeeping)
        self.housekeeping_timer.start(2000)

        self.load_recording_log()
        self.refresh_cameras()
        QTimer.singleShot(300, self.start_capture)

    @staticmethod
    def _video_device_sort_key(path):
        match = re.search(r"(\d+)$", str(path))
        return int(match.group(1)) if match else 999999

    @staticmethod
    def _parse_v4l2_mjpeg_modes(text):
        """Return {"1280x720": [60, 120], ...} for MJPEG modes only."""
        modes = {}
        in_mjpeg = False
        current_resolution = None

        for line in text.splitlines():
            fmt = re.search(r"\[\d+\]:\s+'([^']+)'", line)
            if fmt:
                code = fmt.group(1).upper()
                in_mjpeg = code in ("MJPG", "JPEG")
                current_resolution = None
                continue

            if not in_mjpeg:
                continue

            size = re.search(r"Size:\s+Discrete\s+(\d+)x(\d+)", line)
            if size:
                current_resolution = f"{size.group(1)}x{size.group(2)}"
                modes.setdefault(current_resolution, [])
                continue

            fps_match = re.search(r"\(([\d.]+)\s+fps\)", line, re.I)
            if fps_match and current_resolution:
                fps = int(round(float(fps_match.group(1))))
                if fps > 0 and fps not in modes[current_resolution]:
                    modes[current_resolution].append(fps)

        for resolution in list(modes):
            modes[resolution] = sorted(modes[resolution])
            if not modes[resolution]:
                del modes[resolution]

        return modes

    def _probe_v4l2_cameras(self):
        sources = {}

        if shutil.which("v4l2-ctl") is None:
            return sources

        devices = sorted(
            Path("/dev").glob("video*"),
            key=self._video_device_sort_key,
        )

        for device in devices:
            try:
                result = subprocess.run(
                    [
                        "v4l2-ctl",
                        f"--device={device}",
                        "--list-formats-ext",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=3,
                )
            except Exception:
                continue

            if result.returncode != 0:
                continue

            modes = self._parse_v4l2_mjpeg_modes(result.stdout)
            if not modes:
                # Ignore codec/ISP nodes and camera nodes that do not expose
                # MJPEG. This app records MJPEG without re-encoding.
                continue

            card_name = device.name
            try:
                info = subprocess.run(
                    ["v4l2-ctl", f"--device={device}", "--info"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=2,
                ).stdout
                match = re.search(r"Card type\s*:\s*(.+)", info)
                if match:
                    card_name = match.group(1).strip()
            except Exception:
                pass

            camera_id = f"v4l2:{device}"
            sources[camera_id] = {
                "backend": "v4l2",
                "device": str(device),
                "name": f"{card_name} ({device})",
                "modes": modes,
            }

        return sources

    @staticmethod
    def _common_fps_up_to(max_fps):
        """Integer FPS choices that do not exceed a reported sensor-mode limit."""
        max_int = max(1, int(math.floor(max_fps + 1e-6)))
        common = [5, 10, 15, 20, 24, 25, 30, 40, 50, 60, 75, 90, 100, 120, 144, 180, 200]
        values = [fps for fps in common if fps <= max_int]
        if max_int not in values:
            values.append(max_int)
        return sorted(set(values))

    def _probe_rpicam_cameras(self):
        sources = {}

        binary = (
            shutil.which("rpicam-vid")
            or shutil.which("libcamera-vid")
        )
        if binary is None:
            return sources

        try:
            result = subprocess.run(
                [binary, "--list-cameras"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=8,
            )
        except Exception:
            return sources

        # Depending on rpicam-apps version, the listing can be printed to
        # stdout or stderr.
        text = (result.stdout or "") + "\n" + (result.stderr or "")

        cameras = {}
        current_index = None

        for line in text.splitlines():
            cam = re.match(
                r"\s*(\d+)\s*:\s*([^\[]+?)\s*\[(\d+)x(\d+)\]",
                line,
            )
            if cam:
                current_index = int(cam.group(1))
                model = cam.group(2).strip()
                cameras[current_index] = {
                    "model": model,
                    "native": f"{cam.group(3)}x{cam.group(4)}",
                    "mode_limits": {},
                }
                continue

            if current_index is None:
                continue

            # A sensor-mode line can contain one or more:
            # 1920x1080 [47.57 fps ...]
            for width, height, maxfps in re.findall(
                r"(\d+)x(\d+)\s+\[([\d.]+)\s+fps",
                line,
                re.I,
            ):
                resolution = f"{width}x{height}"
                maximum = float(maxfps)
                old = cameras[current_index]["mode_limits"].get(
                    resolution,
                    0.0,
                )
                cameras[current_index]["mode_limits"][resolution] = max(
                    old,
                    maximum,
                )

        for index, camera in cameras.items():
            modes = {}
            for resolution, maximum in camera["mode_limits"].items():
                modes[resolution] = self._common_fps_up_to(maximum)

            if not modes:
                continue

            camera_id = f"rpicam:{index}"
            sources[camera_id] = {
                "backend": "rpicam",
                "camera_index": index,
                "binary": binary,
                "name": (
                    f"Raspberry Pi CSI: {camera['model']} "
                    f"(camera {index})"
                ),
                "modes": modes,
            }

        return sources

    @staticmethod
    def _resolution_sort_key(text):
        try:
            width, height = [int(x) for x in text.split("x")]
            return (width * height, width, height)
        except Exception:
            return (0, 0, 0)

    def refresh_cameras(self):
        if self.recording:
            QMessageBox.warning(
                self,
                "Recording active",
                "Stop recording before refreshing camera devices.",
            )
            return

        previous_id = self.settings.get("camera_id", "")
        previous_device = self.settings.get("device", "/dev/video0")

        sources = {}
        sources.update(self._probe_v4l2_cameras())
        sources.update(self._probe_rpicam_cameras())
        self.camera_sources = sources

        self.camera_combo.blockSignals(True)
        self.camera_combo.clear()

        for camera_id, source in sources.items():
            self.camera_combo.addItem(source["name"], camera_id)

        if not sources:
            self.camera_combo.addItem("No supported camera found", "")
            self.camera_combo.setEnabled(False)
            self.resolution_combo.clear()
            self.record_fps_combo.clear()
            self.camera_combo.blockSignals(False)

            if hasattr(self, "status_label"):
                self.status_label.setText(
                    "No supported MJPEG camera found. For USB cameras, "
                    "v4l2-ctl must report an MJPG mode. For CSI cameras, "
                    "rpicam-vid/libcamera-vid must detect the sensor."
                )
            return

        self.camera_combo.setEnabled(True)

        target_id = previous_id
        if target_id not in sources:
            # Backward compatibility with the old setting that stored only
            # /dev/video0.
            legacy = f"v4l2:{previous_device}"
            if legacy in sources:
                target_id = legacy
            else:
                target_id = next(iter(sources))

        idx = self.camera_combo.findData(target_id)
        self.camera_combo.setCurrentIndex(max(0, idx))
        self.camera_combo.blockSignals(False)

        self.on_camera_changed()

        if hasattr(self, "status_label"):
            self.status_label.setText(
                f"Detected {len(sources)} supported camera source(s). "
                "Choose one, then click Apply / Restart Camera."
            )

    def _selected_camera_source(self):
        camera_id = self.camera_combo.currentData()
        return self.camera_sources.get(camera_id)

    def on_camera_changed(self, *_):
        source = self._selected_camera_source()
        if source is None:
            return

        previous_resolution = (
            self.settings.get("resolution")
            or self.resolution_combo.currentText()
        )

        resolutions = sorted(
            source["modes"].keys(),
            key=self._resolution_sort_key,
        )

        self.resolution_combo.blockSignals(True)
        self.resolution_combo.clear()
        self.resolution_combo.addItems(resolutions)

        if previous_resolution in resolutions:
            self.resolution_combo.setCurrentText(previous_resolution)
        elif "1280x720" in resolutions:
            self.resolution_combo.setCurrentText("1280x720")
        elif "1920x1080" in resolutions:
            self.resolution_combo.setCurrentText("1920x1080")
        else:
            # Prefer the largest mode not exceeding 1080p; otherwise use the
            # smallest available mode. This avoids accidentally starting a
            # multi-megapixel low-FPS sensor mode.
            under_1080 = []
            for res in resolutions:
                width, height = [int(x) for x in res.split("x")]
                if width <= 1920 and height <= 1080:
                    under_1080.append(res)

            choice = under_1080[-1] if under_1080 else resolutions[0]
            self.resolution_combo.setCurrentText(choice)

        self.resolution_combo.blockSignals(False)
        self.on_resolution_changed()

    def on_resolution_changed(self, *_):
        source = self._selected_camera_source()
        resolution = self.resolution_combo.currentText()

        if source is None or resolution not in source["modes"]:
            return

        fps_values = source["modes"][resolution]
        old_fps = int(self.settings.get("record_fps", 60))

        self.record_fps_combo.blockSignals(True)
        self.record_fps_combo.clear()

        for fps in fps_values:
            self.record_fps_combo.addItem(str(fps))

        if old_fps in fps_values:
            self.record_fps_combo.setCurrentText(str(old_fps))
        elif 60 in fps_values:
            self.record_fps_combo.setCurrentText("60")
        elif 30 in fps_values:
            self.record_fps_combo.setCurrentText("30")
        else:
            self.record_fps_combo.setCurrentText(str(max(fps_values)))

        self.record_fps_combo.blockSignals(False)

    def on_detection_area_changed(self, text):
        roi_active = text == "ROI"
        self.live_label.set_roi_enabled(roi_active)
        self.diff_label.set_roi_enabled(roi_active)
        self.reset_roi_button.setEnabled(roi_active)

        # This callback is also used during GUI construction, before the
        # status label exists. Only update the status text once it has been
        # created.
        if hasattr(self, "status_label"):
            if roi_active:
                self.status_label.setText(
                    "ROI detection active. Drag the yellow rectangle to move it; "
                    "drag edges/corners to resize it."
                )
            else:
                self.status_label.setText(
                    "Full-frame motion detection active."
                )

    def on_roi_changed(self, roi_norm):
        self.diff_label.set_roi(roi_norm)

        # Save immediately so the ROI survives application restarts.
        self.settings["roi_norm"] = [float(v) for v in roi_norm]
        try:
            SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2))
        except Exception:
            pass

    def toggle_armed(self):
        self.armed = not self.armed

        if self.armed:
            self.arm_state.setText("● ARMED")
            self.arm_state.setStyleSheet(
                "font-weight: bold; color: rgb(30, 150, 70);"
            )
            self.arm_button.setText("DISARM")
            self.arm_button.setStyleSheet(
                "font-weight: bold; font-size: 16px; "
                "background-color: rgb(240, 215, 215);"
            )
            self.motion_frames = 0
            self.last_motion_time = 0.0
            self.status_label.setText(
                "ARMED: automatic motion-triggered recording is active."
            )
        else:
            self.arm_state.setText("● DISARMED")
            self.arm_state.setStyleSheet(
                "font-weight: bold; color: rgb(170, 70, 70);"
            )
            self.arm_button.setText("ARM")
            self.arm_button.setStyleSheet(
                "font-weight: bold; font-size: 16px; "
                "background-color: rgb(210, 225, 210);"
            )
            self.motion_frames = 0

            # If motion recording is currently active, disarming requests a
            # clean stop. A deliberately started manual recording is left alone.
            if (
                self.recording
                and not self.manual_recording
                and not self.stop_requested
            ):
                self.request_stop_recording()
                self.status_label.setText(
                    "DISARMED: stopping the active motion recording."
                )
            else:
                self.status_label.setText(
                    "DISARMED: preview remains live; automatic triggers are ignored."
                )

    def choose_save_dir(self):
        path = QFileDialog.getExistingDirectory(
            self,
            "Choose recordings folder",
            self.save_dir_edit.text(),
        )
        if path:
            self.save_dir_edit.setText(path)

    def collect_settings(self):
        source = self._selected_camera_source()
        camera_id = self.camera_combo.currentData() or ""

        device = self.settings.get("device", "/dev/video0")
        if source is not None and source["backend"] == "v4l2":
            device = source["device"]

        self.settings = {
            "device": device,
            "camera_id": camera_id,
            "resolution": self.resolution_combo.currentText(),
            "record_fps": int(self.record_fps_combo.currentText()),
            "preview_fps": int(self.preview_fps_spin.value()),
            "motion_threshold_percent": float(
                self.motion_threshold_spin.value()
            ),
            "pixel_diff_threshold": int(self.pixel_threshold_spin.value()),
            "stop_after_seconds": float(self.stop_after_spin.value()),
            "pretrigger_seconds": float(self.pretrigger_spin.value()),
            "min_motion_frames": int(self.min_motion_frames_spin.value()),
            "save_dir": self.save_dir_edit.text().strip(),
            "detection_area": self.detection_area_combo.currentText(),
            "roi_norm": self.live_label.roi_norm.copy(),
        }

        SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2))

    def apply_and_restart(self):
        if self.recording:
            QMessageBox.warning(
                self,
                "Recording active",
                "Stop the current recording before restarting the camera.",
            )
            return

        self.collect_settings()
        self.start_capture()

    def start_capture(self):
        self.stop_capture()

        self.collect_settings()

        save_dir = Path(self.settings["save_dir"]).expanduser()
        save_dir.mkdir(parents=True, exist_ok=True)

        width, height = [
            int(x) for x in self.settings["resolution"].split("x")
        ]

        preview_width = 480
        preview_height = max(2, int(round(preview_width * height / width)))
        if preview_height % 2:
            preview_height += 1

        run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir = SHM_ROOT / f"run_{run_stamp}_{os.getpid()}"

        if self.run_dir.exists():
            shutil.rmtree(self.run_dir, ignore_errors=True)

        self.run_dir.mkdir(parents=True, exist_ok=True)

        self.processed_segments.clear()
        self.ring_segments.clear()

        source = self._selected_camera_source()
        if source is None:
            self.camera_state.setText("● CAMERA ERROR")
            self.status_label.setText("No camera source is selected.")
            return

        ffmpeg_log = APP_DIR / "ffmpeg_capture.log"
        self.capture_stderr = open(ffmpeg_log, "ab", buffering=0)

        segment_pattern = str(self.run_dir / "seg_%09d.mkv")

        ffmpeg_input = []
        ffmpeg_stdin = None

        if source["backend"] == "v4l2":
            ffmpeg_input = [
                "-f",
                "v4l2",
                "-framerate",
                str(self.settings["record_fps"]),
                "-video_size",
                self.settings["resolution"],
                "-input_format",
                "mjpeg",
                "-i",
                source["device"],
            ]

        elif source["backend"] == "rpicam":
            rpicam_log = APP_DIR / "rpicam_capture.log"
            self.source_stderr = open(rpicam_log, "ab", buffering=0)

            rpicam_cmd = [
                source["binary"],
                "-t",
                "0",
                "-n",
                "--camera",
                str(source["camera_index"]),
                "--codec",
                "mjpeg",
                "--quality",
                "85",
                "--width",
                str(width),
                "--height",
                str(height),
                "--framerate",
                str(self.settings["record_fps"]),
            ]

            if self.settings["record_fps"] > 60:
                # Raspberry Pi's own documentation recommends disabling
                # software colour denoise for high-framerate capture.
                rpicam_cmd.extend(["--denoise", "cdn_off"])

            rpicam_cmd.extend(["-o", "-"])

            try:
                self.source_process = subprocess.Popen(
                    rpicam_cmd,
                    stdout=subprocess.PIPE,
                    stderr=self.source_stderr,
                    bufsize=0,
                )
            except Exception as exc:
                self.source_process = None
                self.camera_state.setText("● CAMERA ERROR")
                self.status_label.setText(
                    f"Could not start Raspberry Pi camera: {exc}"
                )
                return

            ffmpeg_input = [
                "-f",
                "mjpeg",
                "-framerate",
                str(self.settings["record_fps"]),
                "-i",
                "pipe:0",
            ]
            ffmpeg_stdin = self.source_process.stdout

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            *ffmpeg_input,
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            str(self.segment_seconds),
            "-reset_timestamps",
            "1",
            segment_pattern,
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "copy",
            "-f",
            "mjpeg",
            "pipe:1",
        ]

        try:
            self.capture_process = subprocess.Popen(
                cmd,
                stdin=ffmpeg_stdin,
                stdout=subprocess.PIPE,
                stderr=self.capture_stderr,
                bufsize=0,
            )

            # The ffmpeg child owns its duplicated stdin now.
            if self.source_process is not None and self.source_process.stdout:
                self.source_process.stdout.close()

        except Exception as exc:
            self.capture_process = None

            if self.source_process is not None:
                try:
                    self.source_process.terminate()
                except Exception:
                    pass
                self.source_process = None

            self.camera_state.setText("● CAMERA ERROR")
            self.status_label.setText(f"Could not start FFmpeg: {exc}")
            return

        sample_every_n_frames = max(
            1,
            int(
                round(
                    self.settings["record_fps"]
                    / self.settings["preview_fps"]
                )
            ),
        )

        self.preview_thread = PreviewThread(
            self.capture_process.stdout,
            preview_width,
            preview_height,
            sample_every_n_frames,
            self.settings["pixel_diff_threshold"],
        )
        self.preview_thread.frame_ready.connect(self.on_preview_frame)
        self.preview_thread.stream_error.connect(self.on_stream_error)
        self.preview_thread.start()

        self.camera_state.setText("● CAMERA ON")
        self.status_label.setText(
            f"Camera running: {source['name']} — "
            f"{self.settings['resolution']} @ "
            f"{self.settings['record_fps']} fps, preview "
            f"{self.settings['preview_fps']} fps."
        )

    def stop_capture(self):
        if self.preview_thread is not None:
            self.preview_thread.stop()

        # For a CSI camera, stop the producer first so ffmpeg receives EOF.
        if self.source_process is not None:
            try:
                self.source_process.terminate()
                self.source_process.wait(timeout=3)
            except Exception:
                try:
                    self.source_process.kill()
                except Exception:
                    pass

        if self.capture_process is not None:
            try:
                self.capture_process.terminate()
                self.capture_process.wait(timeout=3)
            except Exception:
                try:
                    self.capture_process.kill()
                except Exception:
                    pass

        if self.preview_thread is not None:
            self.preview_thread.wait(1500)

        self.preview_thread = None
        self.capture_process = None
        self.source_process = None

        if self.source_stderr is not None:
            try:
                self.source_stderr.close()
            except Exception:
                pass
            self.source_stderr = None

        if self.capture_stderr is not None:
            try:
                self.capture_stderr.close()
            except Exception:
                pass
            self.capture_stderr = None

        self.camera_state.setText("● CAMERA OFF")

    def on_stream_error(self, message):
        self.camera_state.setText("● CAMERA ERROR")
        self.status_label.setText(
            message + " Check ~/.mxbi_camera_monitor/ffmpeg_capture.log"
        )

    def on_preview_frame(self, frame, mask, changed_fraction):
        # Recalculate motion within the selected detection region.
        if self.detection_area_combo.currentText() == "ROI":
            x, y, rw, rh = self.live_label.roi_norm
            mh, mw = mask.shape

            x1 = max(0, min(mw - 1, int(round(x * mw))))
            y1 = max(0, min(mh - 1, int(round(y * mh))))
            x2 = max(x1 + 1, min(mw, int(round((x + rw) * mw))))
            y2 = max(y1 + 1, min(mh, int(round((y + rh) * mh))))

            roi_mask = mask[y1:y2, x1:x2]
            if roi_mask.size > 0:
                changed_fraction = (
                    float(cv2.countNonZero(roi_mask)) / float(roi_mask.size)
                )
            else:
                changed_fraction = 0.0

        motion_percent = changed_fraction * 100.0

        self.motion_label.setText(f"Motion: {motion_percent:.3f} %")
        self.motion_bar.setValue(
            int(max(0, min(1000, motion_percent * 100)))
        )

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, _ = rgb.shape

        live_image = QImage(
            rgb.data,
            w,
            h,
            3 * w,
            QImage.Format_RGB888,
        ).copy()

        live_pixmap = QPixmap.fromImage(live_image)
        self.live_label.setPixmap(
            live_pixmap.scaled(
                self.live_label.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )

        mh, mw = mask.shape
        diff_image = QImage(
            mask.data,
            mw,
            mh,
            mw,
            QImage.Format_Grayscale8,
        ).copy()

        diff_pixmap = QPixmap.fromImage(diff_image)
        self.diff_label.setPixmap(
            diff_pixmap.scaled(
                self.diff_label.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )

        threshold_fraction = (
            float(self.motion_threshold_spin.value()) / 100.0
        )

        has_motion = changed_fraction >= threshold_fraction

        if has_motion:
            self.motion_frames += 1
            self.last_motion_time = time.monotonic()

            if (
                self.armed
                and self.recording
                and not self.manual_recording
                and self.stop_requested
            ):
                self.stop_requested = False
                self.stop_target_segment = None
                self.status_label.setText(
                    "Motion resumed; continuing the same recording."
                )

            if (
                self.armed
                and not self.recording
                and self.motion_frames
                >= int(self.min_motion_frames_spin.value())
            ):
                self.start_event_recording("motion")

        else:
            self.motion_frames = 0

        if (
            self.recording
            and not self.manual_recording
            and not self.stop_requested
            and self.last_motion_time > 0
            and time.monotonic() - self.last_motion_time
            >= float(self.stop_after_spin.value())
        ):
            self.request_stop_recording()

    def start_manual_recording(self):
        if self.recording:
            return
        self.start_event_recording("manual")

    def start_event_recording(self, trigger):
        if self.recording:
            return

        save_dir = Path(self.save_dir_edit.text()).expanduser()
        save_dir.mkdir(parents=True, exist_ok=True)

        self.event_start_wall = datetime.now()
        self.event_start_monotonic = time.monotonic()

        stamp = self.event_start_wall.strftime("%Y-%m-%d_%H-%M-%S")
        self.event_output = save_dir / f"mxbi_{stamp}.mkv"
        self.event_dir = save_dir / f".mxbi_{stamp}_parts"

        if self.event_dir.exists():
            shutil.rmtree(self.event_dir, ignore_errors=True)

        self.event_dir.mkdir(parents=True, exist_ok=True)

        self.event_segment_count = 0
        self.manual_recording = trigger == "manual"
        self.recording = True
        self.stop_requested = False
        self.stop_target_segment = None

        pretrigger_segments = list(self.ring_segments)
        self.ring_segments.clear()

        for segment in pretrigger_segments:
            if segment.exists():
                target = self.event_dir / segment.name
                try:
                    shutil.move(str(segment), str(target))
                    self.event_segment_count += 1
                except Exception:
                    pass

        self.record_state.setText("● RECORDING")
        self.record_state.setStyleSheet(
            "font-weight: bold; color: rgb(210, 30, 30);"
        )

        if trigger == "motion":
            self.status_label.setText(
                "Motion trigger: recording started."
            )
        else:
            self.status_label.setText(
                "Manual recording started."
            )

        self.current_trigger = trigger

    def request_stop_recording(self):
        if not self.recording or self.stop_requested:
            return

        self.stop_requested = True

        segments = []
        if self.run_dir is not None and self.run_dir.exists():
            segments = sorted(self.run_dir.glob("seg_*.mkv"))

        self.stop_target_segment = segments[-1].name if segments else None

        if self.stop_target_segment is None:
            self.finish_event_recording()
        else:
            self.status_label.setText(
                "Stopping after the current 1-second camera segment closes…"
            )

    def process_segments(self):
        if self.run_dir is None or not self.run_dir.exists():
            return

        files = sorted(self.run_dir.glob("seg_*.mkv"))

        if len(files) < 2:
            return

        finalized = files[:-1]

        for segment in finalized:
            if segment.name in self.processed_segments:
                continue

            self.processed_segments.add(segment.name)

            if self.recording:
                target = self.event_dir / segment.name

                try:
                    shutil.move(str(segment), str(target))
                    self.event_segment_count += 1
                except Exception as exc:
                    self.status_label.setText(
                        f"Could not save segment {segment.name}: {exc}"
                    )

                if (
                    self.stop_requested
                    and segment.name == self.stop_target_segment
                ):
                    self.finish_event_recording()
                    return

            else:
                self.ring_segments.append(segment)

                keep_count = max(
                    1,
                    int(
                        math.ceil(
                            float(self.pretrigger_spin.value())
                            / self.segment_seconds
                        )
                    )
                    + 1,
                )

                while len(self.ring_segments) > keep_count:
                    old = self.ring_segments.popleft()
                    try:
                        old.unlink(missing_ok=True)
                    except Exception:
                        pass

    def finish_event_recording(self):
        if not self.recording:
            return

        self.recording = False
        self.manual_recording = False
        self.stop_requested = False
        self.stop_target_segment = None

        self.record_state.setText("● FINALIZING")
        self.record_state.setStyleSheet(
            "font-weight: bold; color: rgb(180, 120, 0);"
        )

        parts = sorted(self.event_dir.glob("seg_*.mkv"))

        if not parts:
            self.record_state.setText("● NOT RECORDING")
            self.record_state.setStyleSheet("font-weight: bold;")
            self.status_label.setText("No finalized video segments were available.")
            shutil.rmtree(self.event_dir, ignore_errors=True)
            return

        concat_file = self.event_dir / "concat.txt"

        with concat_file.open("w") as f:
            for part in parts:
                safe_path = str(part.resolve()).replace("'", "'\\''")
                f.write(f"file '{safe_path}'\n")

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            str(self.event_output),
        ]

        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
            )

            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip())

            duration = max(
                0.0,
                time.monotonic() - self.event_start_monotonic,
            )

            entry = {
                "start": self.event_start_wall.isoformat(timespec="seconds"),
                "duration_seconds": duration,
                "file": str(self.event_output),
                "trigger": self.current_trigger,
            }

            with LOG_FILE.open("a") as f:
                f.write(json.dumps(entry) + "\n")

            self.add_recording_row(entry)

            self.status_label.setText(
                f"Saved {self.event_output.name}"
            )

        except Exception as exc:
            self.status_label.setText(
                f"Could not finalize recording: {exc}. "
                f"Parts kept in {self.event_dir}"
            )
            self.record_state.setText("● NOT RECORDING")
            self.record_state.setStyleSheet("font-weight: bold;")
            return

        shutil.rmtree(self.event_dir, ignore_errors=True)

        self.record_state.setText("● NOT RECORDING")
        self.record_state.setStyleSheet("font-weight: bold;")

    def add_recording_row(self, entry):
        row = self.table.rowCount()
        self.table.insertRow(row)

        try:
            start_dt = datetime.fromisoformat(entry["start"])
            start_text = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            start_text = entry.get("start", "?")

        duration = float(entry.get("duration_seconds", 0.0))
        minutes = int(duration // 60)
        seconds = duration - 60 * minutes
        duration_text = f"{minutes:02d}:{seconds:04.1f}"

        file_path = Path(entry.get("file", ""))

        self.table.setItem(row, 0, QTableWidgetItem(start_text))
        self.table.setItem(row, 1, QTableWidgetItem(duration_text))
        self.table.setItem(row, 2, QTableWidgetItem(file_path.name))
        self.table.setItem(
            row,
            3,
            QTableWidgetItem(entry.get("trigger", "?")),
        )

        self.table.scrollToBottom()
        self.recording_count_label.setText(
            f"Videos recorded: {self.table.rowCount()}"
        )

    def load_recording_log(self):
        if not LOG_FILE.exists():
            return

        try:
            lines = LOG_FILE.read_text().splitlines()
        except Exception:
            return

        for line in lines[-500:]:
            try:
                self.add_recording_row(json.loads(line))
            except Exception:
                pass

    def update_housekeeping(self):
        save_dir = Path(self.save_dir_edit.text()).expanduser()

        try:
            usage = shutil.disk_usage(save_dir)
            free_gb = usage.free / (1024 ** 3)
            self.disk_label.setText(f"Free disk space: {free_gb:.1f} GB")
        except Exception:
            self.disk_label.setText("Free disk space: —")

        if (
            self.capture_process is not None
            and self.capture_process.poll() is not None
        ):
            self.camera_state.setText("● CAMERA ERROR")
            self.status_label.setText(
                "FFmpeg exited unexpectedly. "
                "Check ~/.mxbi_camera_monitor/ffmpeg_capture.log"
            )

        if (
            self.source_process is not None
            and self.source_process.poll() is not None
            and self.capture_process is not None
            and self.capture_process.poll() is None
        ):
            self.camera_state.setText("● CAMERA ERROR")
            self.status_label.setText(
                "Raspberry Pi camera process exited unexpectedly. "
                "Check ~/.mxbi_camera_monitor/rpicam_capture.log"
            )

    def open_recordings_folder(self):
        path = Path(self.save_dir_edit.text()).expanduser()
        path.mkdir(parents=True, exist_ok=True)

        subprocess.Popen(
            ["xdg-open", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def closeEvent(self, event):
        if self.recording:
            answer = QMessageBox.question(
                self,
                "Recording active",
                "A recording is active. Stop and close?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )

            if answer != QMessageBox.Yes:
                event.ignore()
                return

            self.request_stop_recording()

            # Give the segmenter up to ~2 seconds to close the current segment.
            deadline = time.time() + 2.5
            while self.recording and time.time() < deadline:
                QApplication.processEvents()
                self.process_segments()
                time.sleep(0.05)

        self.stop_capture()

        for segment in list(self.ring_segments):
            try:
                segment.unlink(missing_ok=True)
            except Exception:
                pass

        if self.run_dir is not None:
            shutil.rmtree(self.run_dir, ignore_errors=True)

        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MXBICameraMonitor()
    window.show()
    sys.exit(app.exec_())
