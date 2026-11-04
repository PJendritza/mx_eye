"""Recover from isolated malformed raw JPEG buffers without inventing samples."""

import json
import logging
import time
from pathlib import Path

import av
import numpy as np

logger = logging.getLogger(__name__)


class CameraPacketGuard:
    timeout_ns = 2_000_000_000

    def __init__(self, path, stats):
        self.path = Path(path)
        self.stats = stats
        self.last_good_ns = time.monotonic_ns()
        self.attempt = 0
        self.decoder = av.CodecContext.create("mjpeg", "r")
        self.decoder.options = {"err_detect": "explode"}

    def check(
        self, frame, acquired_ns, monotonic_ns, recording_generation=0, mark_good=True
    ):
        self.attempt += 1
        flat = (
            frame.reshape(-1)
            if isinstance(frame, np.ndarray)
            else np.array([], dtype=np.uint8)
        )
        reason = None
        if flat.dtype != np.uint8 or flat.size < 4 or tuple(flat[:2]) != (255, 216):
            reason = "missing_jpeg_start_or_unexpected_buffer"
        else:
            end = (
                flat.size - 2
                if tuple(flat[-2:]) == (255, 217)
                else flat.tobytes().rfind(b"\xff\xd9")
            )
            if end < 2:
                reason = "missing_jpeg_end"
        if reason is None:
            if mark_good:
                self.last_good_ns = monotonic_ns
            return flat[: end + 2]

        return self.reject(
            frame, acquired_ns, monotonic_ns, recording_generation, reason
        )

    def validate(self, frame, acquired_ns, monotonic_ns, recording_generation=0):
        packet = self.check(
            frame, acquired_ns, monotonic_ns, recording_generation, mark_good=False
        )
        if packet is None:
            return None
        try:
            frames = self.decoder.decode(av.Packet(packet.tobytes()))
            if len(frames) != 1:
                raise ValueError("JPEG did not decode to one image")
            pixels = frames[0].to_ndarray(format="bgr24")
        except (av.FFmpegError, ValueError) as exc:
            return self.reject(
                packet,
                acquired_ns,
                monotonic_ns,
                recording_generation,
                f"jpeg_decode_failed: {exc}",
            )
        self.last_good_ns = monotonic_ns
        return packet, pixels

    def reject(self, frame, acquired_ns, monotonic_ns, recording_generation, reason):
        flat = (
            frame.reshape(-1)
            if isinstance(frame, np.ndarray)
            else np.array([], dtype=np.uint8)
        )
        self.stats["camera_rejected"].value += 1
        if recording_generation:
            self.stats["record_camera_rejected"].value += 1
        row = {
            "kind": "camera_frame_rejected",
            "timestamp_ns": acquired_ns,
            "acquisition_monotonic_ns": monotonic_ns,
            "read_attempt": self.attempt,
            "recording_generation": recording_generation,
            "reason": reason,
            "bytes": int(flat.nbytes),
            "shape": list(frame.shape) if isinstance(frame, np.ndarray) else None,
            "dtype": str(flat.dtype),
            "first_bytes": flat[:8].tobytes().hex(),
            "last_bytes": flat[-8:].tobytes().hex(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")
        count = int(self.stats["camera_rejected"].value)
        if count == 1 or count % 100 == 0:
            logger.warning("Camera frames rejected: %s; details: %s", count, self.path)
        if monotonic_ns - self.last_good_ns >= self.timeout_ns:
            raise RuntimeError(
                "No valid camera JPEG for two seconds; acquisition stopped. Check camera diagnostics."
            )


def save_recording_rejections(source, destination, generation):
    """Copy this recording's diagnostics after capture acknowledges its boundary."""
    with Path(destination).open("w", encoding="utf-8") as output:
        if source and Path(source).exists():
            with Path(source).open(encoding="utf-8") as stream:
                for line in stream:
                    # Capture can already be appending a later, unrelated row.
                    if (
                        line.endswith("\n")
                        and json.loads(line)["recording_generation"] == generation
                    ):
                        output.write(line)
