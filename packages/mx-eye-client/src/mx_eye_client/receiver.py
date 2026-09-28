"""Small simulated MXBI consumer. All communication goes through the SDK."""

import concurrent.futures
import math
import time
from collections import deque

import numpy as np
import pyqtgraph as pg
from mx_eye_protocol.control import StatusSnapshot
from py_mx_eye import MxEye, MxEyeConfig
from PySide6 import QtCore as C
from PySide6 import QtWidgets as W

from .style import label


class Receiver(W.QWidget):
    def __init__(self, host, data_port, control_port):
        super().__init__()
        self.client = None
        self.data_port, self.control_port = data_port, control_port
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.pending = None
        self.history = deque(maxlen=4000)
        self.session = None
        self.coordinate_system = None
        self.newest = None
        self.counted = 0
        self.last = (time.monotonic(), 0)
        self.rate = 0
        self.connected = False
        self.next_status = 0
        self.setWindowTitle("mx_eye · Receiver demo")
        self.resize(920, 620)
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        row = W.QHBoxLayout()
        row.addWidget(label("mx_eye receiver", "brand"))
        row.addStretch()
        self.host = W.QLineEdit(host)
        self.host.setMaximumWidth(170)
        row.addWidget(self.host)
        self.connect_button = W.QPushButton("Connect")
        self.connect_button.clicked.connect(self.connect_tracker)
        row.addWidget(self.connect_button)
        self.start_button = W.QPushButton("Start tracker")
        self.start_button.clicked.connect(lambda: self.run(self.client.start))
        row.addWidget(self.start_button)
        self.stop_button = W.QPushButton("Stop tracker")
        self.stop_button.clicked.connect(lambda: self.run(self.client.stop))
        row.addWidget(self.stop_button)
        layout.addLayout(row)
        layout.addWidget(
            label(
                "Simulated MXBI consumer · synchronous reads on this thread; plots refresh at 25 Hz",
                "muted",
            )
        )
        self.metrics = label("Connect to the tracker to begin.", "metric")
        self.metrics.setWordWrap(True)
        layout.addWidget(self.metrics)
        pg.setConfigOptions(antialias=False, background="#10151d", foreground="#96a9c2")
        self.plot = pg.PlotWidget(title="Received eye signal · pixels")
        self.plot.addLegend()
        self.plot.setLabel("bottom", "Time", "s")
        self.plot.setLabel("left", "Eye signal", "px")
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.x = self.plot.plot(pen=pg.mkPen("#68c9f3", width=1.4), name="x")
        self.y = self.plot.plot(pen=pg.mkPen("#e9a968", width=1.4), name="y")
        layout.addWidget(self.plot, 2)
        self.delayplot = pg.PlotWidget(
            title="Host acquisition → receiver · estimated ms"
        )
        self.delayplot.setLabel("left", "Delay", "ms")
        self.delay = self.delayplot.plot(pen=pg.mkPen("#83d9b0", width=1.4))
        layout.addWidget(self.delayplot, 1)
        self.info = label(
            "Delay compares frame timestamps with this computer's clock, so both ends must share one clock domain (same host, or NTP/PTP). Camera exposure / USB latency is not measured.",
            "muted",
        )
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        self.state = label("Disconnected", "muted")
        layout.addWidget(self.state)
        self.timer = C.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(40)
        self.refresh()

    def run(self, function):
        if self.pending is None:
            self.pending = self.executor.submit(function)

    def connect_tracker(self):
        if self.client:
            self.client.close()
        self.client = MxEye(
            MxEyeConfig(
                host=self.host.text().strip(),
                data_port=self.data_port,
                control_port=self.control_port,
            )
        )
        self.connected = False
        self.counted = 0
        self.newest = None
        self.run(self.client.connect)

    def record(self, sample):
        """Append one received sample to the trace, restarting on a new session."""
        payload = sample.frame.payload
        if (
            payload.session != self.session
            or payload.coordinate_system != self.coordinate_system
        ):
            self.history.clear()
            self.session = payload.session
            self.coordinate_system = payload.coordinate_system
            self.plot.setTitle(
                payload.coordinate_system.replace("_", " ") + " · pixels"
            )
        self.history.append(
            (
                sample.receive_ns / 1e9,
                payload.x,
                payload.y,
                sample.arrival_age_ms,
            )
        )

    def refresh(self):
        if self.pending and self.pending.done():
            try:
                reply = self.pending.result()
                if isinstance(reply, MxEye):
                    self.connected = True
                    self.state.setText("Connected")
                elif isinstance(reply, StatusSnapshot):
                    self.state.setText(f"{reply.state} · {reply.message}")
            except Exception as exc:  # noqa: BLE001 - report failures in the UI label
                self.state.setText(str(exc))
            self.pending = None
        if (
            self.connected
            and self.pending is None
            and time.monotonic() >= self.next_status
        ):
            self.next_status = time.monotonic() + 1
            self.run(self.client.status)
        self.connect_button.setEnabled(self.pending is None)
        for button in (self.start_button, self.stop_button):
            button.setEnabled(self.client is not None and self.pending is None)
        if not self.connected:
            return
        now = time.monotonic()
        newest = None
        try:
            # Diagnostic mode takes every sample received, valid or not, and
            # timeout 0 keeps this GUI thread from ever blocking.
            for sample in self.client.read(
                timeout=0.0, max_age_ms=None, require_valid=False
            ):
                newest = sample
                self.counted += 1
                self.record(sample)
        except (ConnectionError, RuntimeError) as exc:
            self.connected = False
            self.state.setText(str(exc))
        last, count = self.last
        if now - last > 0.5:
            self.rate = (self.counted - count) / (now - last)
            self.last = (now, self.counted)
        sample = newest if newest is not None else self.newest
        self.newest = sample

        def number(value):
            return f"{value:.2f}" if math.isfinite(value) else "—"

        if sample is not None:
            stale = not math.isfinite(sample.age_ms) or sample.age_ms > 100
            validity = (
                "STALE"
                if stale
                else ("VALID" if sample.frame.payload.valid else "LOST")
            )
            self.metrics.setText(
                f"{validity}     {self.rate:.1f} Hz     PROCESS {number(sample.frame.payload.processing_ms)} ms     NETWORK ≈{number(sample.network_ms)} ms     AGE NOW ≈{number(sample.age_ms)} ms"
            )
        self.info.setText(
            "Delay is measured from host read-return timestamps shared by both ends; it excludes exposure and camera/USB buffering. Raw, uncalibrated image-pixel signal."
        )
        if self.history:
            data = np.asarray(self.history)
            # Sample timestamps and this window share the tracker's wall clock.
            wall = time.time_ns() / 1e9
            data = data[data[:, 0] >= wall - 8]
            t = data[:, 0] - wall
            self.x.setData(t, data[:, 1], connect="finite")
            self.y.setData(t, data[:, 2], connect="finite")
            self.delay.setData(t, data[:, 3], connect="finite")
            self.plot.setXRange(-8, 0, padding=0)
            self.delayplot.setXRange(-8, 0, padding=0)

    def closeEvent(self, event):
        self.timer.stop()
        # Closing the receiver does not stop the tracker/recording remotely.
        self.executor.shutdown(wait=True, cancel_futures=True)
        if self.client:
            self.client.close()
        event.accept()
