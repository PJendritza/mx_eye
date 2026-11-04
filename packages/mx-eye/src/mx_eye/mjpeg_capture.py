"""Windows camera stream copy through FFmpeg, with framed packets over a pipe."""

import os
import shutil
import subprocess
from collections import deque
from threading import Event, Thread

import av
import cv2
import numpy as np


def dshow_device(source, names=None):
    """Keep the selected index when friendly names are duplicated."""
    if names is None:
        from pygrabber.dshow_graph import FilterGraph

        names = FilterGraph().get_input_devices()
    index = source.get("camera", 0)
    name = source.get("camera_name")
    if name and names.count(name) == 1:
        return name, 0  # A uniquely named device may have moved to another index.
    if not 0 <= index < len(names) or (name and names[index] != name):
        raise RuntimeError(
            "Selected camera is missing or ambiguous. Refresh Camera Settings and select it again."
        )
    name = names[index]
    return name, names[:index].count(name)


class PacketPipe:
    """Expose read-only streaming I/O; Windows pipe seek() is not usable."""

    def __init__(self, stream):
        self.stream = stream

    def read(self, size):
        return self.stream.read(size)


class MjpegCapture:
    """Small VideoCapture-compatible adapter; FFmpeg never decodes/re-encodes.

    NUT transports packet boundaries, avoiding guesses based on JPEG markers.
    Timestamps used by MX Eye remain host read-return times, including pipe delay.
    """

    def __init__(self, source, stop=None):
        executable = shutil.which("ffmpeg")
        if not executable:
            raise RuntimeError(
                "MJPEG capture on Windows requires ffmpeg on PATH. Install FFmpeg or select MSMF for the decoded FFV1 fallback."
            )
        name, occurrence = dshow_device(source)
        command = [
            executable,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-f",
            "dshow",
            "-video_size",
            f"{source['width']}x{source['height']}",
            "-framerate",
            str(source["fps"]),
            "-vcodec",
            "mjpeg",
            "-video_device_number",
            str(occurrence),
            "-i",
            f"video={name}",
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "copy",
            "-f",
            "nut",
            "-flush_packets",
            "1",
            "pipe:1",
        ]
        self.messages = deque(maxlen=20)
        self.container = None
        self.stop = stop
        self.closed = Event()
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.stderr_thread = Thread(target=self._drain_errors, daemon=True)
        self.stderr_thread.start()
        self.cancel_thread = Thread(target=self._cancel_on_stop, daemon=True)
        self.cancel_thread.start()
        try:
            self.container = av.open(
                PacketPipe(self.process.stdout),
                format="nut",
                buffer_size=4096,
                options={"probesize": "32", "analyzeduration": "0"},
            )
            stream = self.container.streams.video[0]
            if stream.codec_context.name != "mjpeg":
                raise RuntimeError("Camera did not provide MJPEG packets")
            self.properties = {
                cv2.CAP_PROP_FRAME_WIDTH: stream.codec_context.width,
                cv2.CAP_PROP_FRAME_HEIGHT: stream.codec_context.height,
                cv2.CAP_PROP_FPS: float(stream.average_rate or source["fps"]),
                cv2.CAP_PROP_FOURCC: cv2.VideoWriter_fourcc("M", "J", "P", "G"),
                cv2.CAP_PROP_CONVERT_RGB: 0,
            }
            self.packets = iter(self.container.demux(stream))
        except Exception as exc:
            self.release()
            raise RuntimeError(
                f"MJPEG camera capture failed: {exc}. {' '.join(self.messages)}"
            ) from exc

    def _drain_errors(self):
        for line in iter(self.process.stderr.readline, b""):
            self.messages.append(line.decode(errors="replace").strip())

    def _cancel_on_stop(self):
        while not self.closed.wait(0.05) and self.process.poll() is None:
            if self.stop is not None and self.stop.is_set():
                try:
                    self.process.terminate()
                except OSError:
                    pass  # It already exited.
                return

    def isOpened(self):
        return self.container is not None

    def get(self, prop):
        return self.properties.get(prop, 0)

    def set(self, prop, value):
        # Mode was selected on open. Live camera controls are not supported here.
        return prop in self.properties and self.properties[prop] == value

    def read(self):
        for packet in self.packets:
            if self.stop is not None and self.stop.is_set():
                return False, None
            if packet.size:
                return True, np.frombuffer(bytes(packet), dtype=np.uint8)
        if self.stop is not None and self.stop.is_set():
            return False, None
        raise RuntimeError(f"MJPEG camera stream ended. {' '.join(self.messages)}")

    def release(self):
        self.closed.set()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        if self.container is not None:
            self.container.close()
            self.container = None
        self.stderr_thread.join(timeout=1)
        self.cancel_thread.join(timeout=1)
        for pipe in (self.process.stdout, self.process.stderr):
            if pipe:
                pipe.close()
