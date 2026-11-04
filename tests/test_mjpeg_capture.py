import shutil
import subprocess
import threading

import av
import numpy as np
import pytest
from mx_eye.camera_diagnostics import CameraPacketGuard
from mx_eye.mjpeg_capture import MjpegCapture, dshow_device
from mx_eye.recording import TimestampedVideoWriter
from test_camera_recovery import counters


def test_packet_transport_validation_and_unchanged_recording(tmp_path, monkeypatch):
    """Use a synthetic MJPEG source through the actual FFmpeg pipe transport."""
    from mx_eye import mjpeg_capture

    executable = shutil.which("ffmpeg")
    if not executable:
        pytest.skip("FFmpeg is required for Windows packet capture")
    original_popen = subprocess.Popen
    camera_commands = []

    def synthetic_camera(command, **kwargs):
        camera_commands.append(command)
        return original_popen(
            [
                executable,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=64x48:rate=30",
                "-frames:v",
                "8",
                "-c:v",
                "mjpeg",
                "-pix_fmt",
                "yuvj422p",
                "-f",
                "nut",
                "pipe:1",
            ],
            **kwargs,
        )

    monkeypatch.setattr(mjpeg_capture.subprocess, "Popen", synthetic_camera)
    monkeypatch.setattr(
        mjpeg_capture, "dshow_device", lambda source: ("test camera", 1)
    )
    cap = MjpegCapture(
        {"camera_name": "test camera", "width": 64, "height": 48, "fps": 30}
    )
    guard = CameraPacketGuard(tmp_path / "diagnostics.jsonl", counters())
    writer = TimestampedVideoWriter(tmp_path / "video.mkv", 30)
    originals = []
    try:
        for index in range(8):
            ok, packet = cap.read()
            assert ok
            validated = guard.validate(packet, index, guard.last_good_ns + 1_000_000)
            assert validated is not None
            original, pixels = validated
            assert pixels.shape == (48, 64, 3)
            originals.append(original.tobytes())
            writer.write(original, 1_000_000_000 + index * 33_333_333)
    finally:
        writer.release()
        cap.release()
    command = camera_commands[0]
    assert command[command.index("-vcodec") + 1] == "mjpeg"
    assert command[command.index("-c:v") + 1] == "copy"
    assert command[command.index("-video_device_number") + 1] == "1"
    assert cap.process.poll() is not None
    assert not cap.stderr_thread.is_alive()
    assert not cap.cancel_thread.is_alive()
    with av.open(str(tmp_path / "video.mkv")) as video:
        assert [bytes(p) for p in video.demux(video=0) if p.size] == originals
    with av.open(str(tmp_path / "video.mkv")) as video:
        images = [f.to_ndarray(format="bgr24") for f in video.decode(video=0)]
    assert len(images) == 8
    assert np.abs(images[0][:, :, 0].astype(int) - images[0][:, :, 2]).mean() > 10

    stopped = threading.Event()
    second = MjpegCapture(
        {"camera_name": "test camera", "width": 64, "height": 48, "fps": 30},
        stop=stopped,
    )
    try:
        stopped.set()
        assert second.read() == (False, None)
    finally:
        second.release()
    assert second.process.poll() is not None


def test_missing_ffmpeg_has_actionable_error(monkeypatch):
    monkeypatch.setattr("mx_eye.mjpeg_capture.shutil.which", lambda name: None)
    with pytest.raises(RuntimeError, match="requires ffmpeg"):
        MjpegCapture({})


def test_duplicate_camera_names_preserve_selection():
    names = ["same", "other", "same"]
    assert dshow_device({"camera": 2, "camera_name": "same"}, names) == ("same", 1)
    assert dshow_device({"camera": 0, "camera_name": "same"}, names) == ("same", 0)
    assert dshow_device({"camera": 2}, names) == ("same", 1)
    assert dshow_device({"camera": 0, "camera_name": "other"}, names) == ("other", 0)
    with pytest.raises(RuntimeError, match="ambiguous"):
        dshow_device({"camera": 1, "camera_name": "same"}, names)
    with pytest.raises(RuntimeError, match="missing"):
        dshow_device({"camera": 5}, names)
