"""Small codec/timeline checks using synthetic frames, without camera hardware."""
import threading

import av
import cv2
import numpy as np
import pytest

from mx_eye.recording import TimestampedVideoWriter
from mx_eye.video import PlaybackClock, VideoReader


@pytest.mark.parametrize("compressed", [False, True])
def test_mkv_preserves_irregular_timing_and_images(tmp_path, compressed):
    path = tmp_path / "video.mkv"
    offsets = [0, 8_000_000, 19_000_000, 50_000_000, 67_000_000]
    random = np.random.default_rng(42)
    images = [random.integers(0, 256, (48, 64, 3), dtype=np.uint8) for _ in offsets]
    source = [cv2.imencode(".jpg", image)[1].reshape(-1) for image in images] if compressed else images
    writer = TimestampedVideoWriter(path, 120)
    for image, offset in zip(source, offsets):
        writer.write(image, 9_000_000_000 + offset)
    writer.release()
    with av.open(str(path)) as video:
        frames = list(video.decode(video=0))
    assert len(frames) == len(images)
    actual = [round(frame.pts * frame.time_base * 1e9) for frame in frames]
    assert actual == pytest.approx(offsets, abs=1_000_000)
    if compressed:
        with av.open(str(path)) as video:
            packets = [bytes(p) for p in video.demux(video=0) if p.size]
        assert packets == [image.tobytes() for image in source]
    else:
        for frame, image in zip(frames, images):
            np.testing.assert_array_equal(frame.to_ndarray(format="bgr24"), image)
    reader = VideoReader(str(path), threading.Event())
    try:
        assert reader.timing == "Acquisition timestamps"
        assert reader.read(3)[2] == 50_000_000
        assert reader.read(1)[2] == 8_000_000
        assert reader.read(4)[2] == 67_000_000
    finally:
        reader.close()


def test_playback_clock_respects_intervals_speed_and_resets():
    clock = PlaybackClock()
    assert clock.delay(0, 10, 1) == 0
    assert clock.delay(100_000_000, 10.02, 1) == pytest.approx(.08)
    clock.reset()
    assert clock.delay(0, 20, 2) == 0
    assert clock.delay(100_000_000, 20.01, 2) == pytest.approx(.04)
    # Slow processing rebases rather than dropping or rushing later frames.
    assert clock.delay(200_000_000, 20.2, 2) == 0
    assert clock.delay(300_000_000, 20.21, 2) == pytest.approx(.04)
    assert clock.measured_speed(0, 30) is None
    assert clock.measured_speed(1_000_000_000, 32) == pytest.approx(.5)
    clock.reset()
    assert clock.measured_speed(5_000_000_000, 50) is None


def test_video_worker_uses_timestamps_not_nominal_fps(tmp_path):
    import socket
    import time
    from mx_eye import config as cfg
    from mx_eye.service import Service

    path = tmp_path / "paced.mkv"
    writer = TimestampedVideoWriter(path, 120)
    for index in range(16):
        writer.write(np.full((48, 64, 3), index, np.uint8), 1_000_000_000 + index * 100_000_000)
    writer.release()
    config = cfg.MxEyeConfigStore.defaults()
    config.value.source.mode = cfg.SourceMode.VIDEO
    config.value.source.path = str(path)
    for key in ("control_port", "data_port"):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            setattr(config.value.network, key, sock.getsockname()[1])
    service = Service(config)
    try:
        service.submit("start").result(timeout=15)
        started = time.monotonic()
        deadline = started + 6
        while not service.snapshot().paused and time.monotonic() < deadline:
            time.sleep(.01)
        elapsed = time.monotonic() - started
        state = service.snapshot()
        assert state.paused, state
        assert state.stats.acquired == 16
        assert elapsed >= 1.3, "Playback incorrectly followed nominal 120 FPS"
        assert state.stats.capture_fault == 0
        assert state.stats.tracking_fault == 0
        assert service.video_timing_info == "Acquisition timestamps"
    finally:
        service.close()
