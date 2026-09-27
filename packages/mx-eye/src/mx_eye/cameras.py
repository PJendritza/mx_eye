"""Camera discovery in an isolated process; Windows uses DirectShow capabilities."""

import multiprocessing as mp
import queue
import sys
import time

from PySide6 import QtCore as C
from PySide6 import QtWidgets as W

from .config import CameraBackend, SourceConfig


def discover_cameras(output):
    try:
        if sys.platform != "win32":
            raise RuntimeError(
                "Automatic camera modes currently require Windows. Use manual camera settings on this platform."
            )
        try:
            from pygrabber.dshow_graph import FilterGraph
        except ImportError:
            raise RuntimeError(
                "Install camera discovery with: python -m pip install pygrabber"
            )
        graph = FilterGraph()
        names = graph.get_input_devices()
        cameras = []
        for index, name in enumerate(names):
            graph = FilterGraph()
            formats = []
            error = ""
            try:
                graph.add_video_input_device(index)
                for mode in graph.get_input_device().get_formats():
                    width, height = abs(int(mode["width"])), abs(int(mode["height"]))
                    if not (32 <= width <= 16384 and 32 <= height <= 16384):
                        continue
                    # pygrabber versions can interchange the interval-derived rate labels.
                    fps = max(
                        float(mode["min_framerate"]), float(mode["max_framerate"])
                    )
                    subtype = (
                        mode["media_type_str"].removeprefix("MEDIASUBTYPE_").upper()
                    )
                    fourcc = {"RGB24": "BGR3", "RGB32": "BGR4", "YUYV": "YUY2"}.get(
                        subtype, subtype
                    )
                    formats.append(
                        dict(width=width, height=height, fps=fps, fourcc=fourcc)
                    )
            except Exception as exc:
                error = str(exc)
            finally:
                graph.remove_filters()
            cameras.append(dict(index=index, name=name, formats=formats, error=error))
        output.put((cameras, ""))
    except Exception as exc:
        output.put(([], str(exc)))


class CameraControls(W.QWidget):
    def __init__(self, source, parent=None):
        super().__init__(parent)
        self.source = source.model_copy(deep=True)
        self.cameras = []
        self.process = None
        self.output = None
        form = W.QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        row = W.QHBoxLayout()
        self.camera = W.QComboBox()
        self.camera.addItem(f"Camera {source.camera} (saved index)", source.camera)
        self.refresh = W.QPushButton("Refresh")
        row.addWidget(self.camera, 1)
        row.addWidget(self.refresh)
        form.addRow("Detected camera", row)
        self.resolution = W.QComboBox()
        self.resolution.addItem(
            f"{source.width} × {source.height} (saved)", (source.width, source.height)
        )
        form.addRow("Resolution", self.resolution)
        self.format = W.QComboBox()
        form.addRow("Camera format", self.format)
        self.info = W.QLabel("Scanning connected cameras…")
        self.info.setWordWrap(True)
        form.addRow(self.info)
        self.fps = W.QDoubleSpinBox()
        self.fps.setRange(1, 1000)
        self.fps.setValue(source.fps)
        form.addRow("Requested FPS", self.fps)
        self.manual = W.QGroupBox("Manual camera settings")
        manual = W.QFormLayout(self.manual)
        self.index = W.QSpinBox()
        self.index.setRange(0, 99)
        self.index.setValue(source.camera)
        self.width = W.QSpinBox()
        self.height = W.QSpinBox()
        for widget, key in [(self.width, "width"), (self.height, "height")]:
            widget.setRange(32, 16384)
            widget.setValue(getattr(source, key))
        self.backend = W.QComboBox()
        self.backend.addItems([backend.value for backend in CameraBackend])
        self.backend.setCurrentText(source.backend.value)
        self.fourcc = W.QLineEdit(source.fourcc)
        for text, widget in [
            ("Index", self.index),
            ("Width", self.width),
            ("Height", self.height),
            ("Backend", self.backend),
            ("Format", self.fourcc),
        ]:
            manual.addRow(text, widget)
        form.addRow(self.manual)
        self.camera.currentIndexChanged.connect(self.camera_changed)
        self.resolution.currentIndexChanged.connect(self.resolution_changed)
        self.format.currentIndexChanged.connect(self.format_changed)
        self.refresh.clicked.connect(self.scan)
        self.timer = C.QTimer(self)
        self.timer.timeout.connect(self.poll)
        C.QTimer.singleShot(0, self.scan)

    def scan(self):
        self.stop_scan()
        self.refresh.setEnabled(False)
        self.camera.setEnabled(False)
        self.resolution.setEnabled(False)
        self.format.setEnabled(False)
        self.info.setText("Scanning connected cameras…")
        context = mp.get_context("spawn")
        self.output = context.Queue()
        self.process = context.Process(
            target=discover_cameras, args=(self.output,), daemon=True
        )
        self.process.start()
        self.started = time.monotonic()
        self.timer.start(100)

    def poll(self):
        try:
            cameras, error = self.output.get_nowait()
        except queue.Empty:
            if time.monotonic() - self.started < 20 and self.process.is_alive():
                return
            cameras, error = (
                [],
                "Camera discovery did not finish. Close other camera apps and refresh, or use manual settings.",
            )
        selected = self.camera.currentData()
        previous = next(
            (item for item in self.cameras if item["index"] == selected), None
        )
        selected_name = previous["name"] if previous else ""
        selected_size = self.resolution.currentData()
        self.stop_scan()
        self.refresh.setEnabled(True)
        self.camera.setEnabled(True)
        self.resolution.setEnabled(True)
        self.format.setEnabled(True)
        self.cameras = cameras
        if error or not cameras:
            self.info.setText(
                error or "No cameras detected. Plug in a camera and click Refresh."
            )
            with C.QSignalBlocker(self.camera):
                self.camera.clear()
            self.resolution.clear()
            self.format.clear()
            self.manual.setVisible(True)
            return
        with C.QSignalBlocker(self.camera):
            self.camera.clear()
            for camera in cameras:
                self.camera.addItem(
                    f"{camera['name']} · {camera['index']}", camera["index"]
                )
            matches = [
                i for i, item in enumerate(cameras) if item["name"] == selected_name
            ]
            self.camera.setCurrentIndex(
                matches[0]
                if len(matches) == 1
                else max(0, self.camera.findData(selected))
            )
        current = cameras[self.camera.currentIndex()]
        self.camera_changed(
            preferred=selected_size if current["name"] == selected_name else None
        )

    def camera_changed(self, *args, preferred=None):
        camera = next(
            (
                item
                for item in self.cameras
                if item["index"] == self.camera.currentData()
            ),
            None,
        )
        if camera is None:
            return
        self.index.setValue(camera["index"])
        self.backend.setCurrentText(CameraBackend.DSHOW.value)
        usable = [m for m in camera["formats"] if len(m["fourcc"]) == 4]
        modes = usable or camera["formats"]
        sizes = sorted(
            {(m["width"], m["height"]) for m in modes},
            key=lambda s: (s[0] * s[1], s[0]),
            reverse=True,
        )
        fast_sizes = {
            s
            for s in sizes
            if any(m["fps"] >= 29 and (m["width"], m["height"]) == s for m in modes)
        }
        default_size = next(
            (s for s in sizes if s in fast_sizes), sizes[0] if sizes else None
        )
        if not fast_sizes and modes:
            fastest = max(m["fps"] for m in modes)
            default_size = next(
                s
                for s in sizes
                if any(
                    m["fps"] == fastest and (m["width"], m["height"]) == s
                    for m in modes
                )
            )
        with C.QSignalBlocker(self.resolution):
            self.resolution.clear()
            for w, h in sizes:
                self.resolution.addItem(f"{w} × {h}", (w, h))
            if preferred is not None and tuple(preferred) in fast_sizes:
                self.resolution.setCurrentIndex(sizes.index(tuple(preferred)))
            elif default_size is not None:
                self.resolution.setCurrentIndex(sizes.index(default_size))
        self.manual.setVisible(not sizes)
        self.info.setText(
            "Highest resolution reporting at least 29 fps selected."
            if fast_sizes
            else (
                "No mode reports 29 fps; fastest reported mode selected."
                if sizes
                else "This camera did not report its modes. Use manual settings. "
                + camera["error"]
            )
        )
        self.resolution_changed()

    def resolution_changed(self):
        size = self.resolution.currentData()
        if not size:
            return
        self.width.setValue(size[0])
        self.height.setValue(size[1])
        camera = next(
            (
                item
                for item in self.cameras
                if item["index"] == self.camera.currentData()
            ),
            None,
        )
        with C.QSignalBlocker(self.format):
            self.format.clear()
        if camera:
            modes = [
                m for m in camera["formats"] if (m["width"], m["height"]) == tuple(size)
            ]
            usable = [m for m in modes if len(m["fourcc"]) == 4]
            by_format = {}
            for mode in usable:
                code = mode["fourcc"]
                if code not in by_format or mode["fps"] > by_format[code]["fps"]:
                    by_format[code] = mode
            options = sorted(
                by_format.values(),
                key=lambda m: (m["fps"], m["fourcc"] == "MJPG"),
                reverse=True,
            )
            with C.QSignalBlocker(self.format):
                for mode in options:
                    self.format.addItem(
                        f"{mode['fourcc']} · up to {mode['fps']:g} fps", mode
                    )
            self.format.setEnabled(bool(options))
            self.manual.setVisible(not options)
            self.format_changed()

    def format_changed(self):
        mode = self.format.currentData()
        if not mode:
            return
        self.fourcc.setText(mode["fourcc"])
        self.fps.setValue(max(1, mode["fps"]))
        self.info.setText(
            f"{mode['width']} × {mode['height']} · {mode['fourcc']} · driver reports up to {mode['fps']:.1f} fps. Check measured ACQ after starting."
        )

    def values(self):
        return SourceConfig(
            mode=self.source.mode,
            camera=self.index.value(),
            path=self.source.path,
            width=self.width.value(),
            height=self.height.value(),
            fps=self.fps.value(),
            backend=CameraBackend(self.backend.currentText()),
            fourcc=self.fourcc.text().strip(),
            speed=self.source.speed,
        )

    def stop_scan(self):
        self.timer.stop()
        if self.process is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=0.5)
            self.process = None
        if self.output is not None:
            self.output.close()
            self.output = None
