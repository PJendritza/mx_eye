import json
import queue
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np
import pytest
from mx_eye import config as cfg
from mx_eye.camera_diagnostics import CameraPacketGuard, save_recording_rejections
from mx_eye.pipeline import capture_worker
from mx_eye.service import STAT_NAMES


def counters():
    return {name: SimpleNamespace(value=0) for name in (*STAT_NAMES, "record_acquired")}


def test_rejection_timeout_resets_after_good_packet(tmp_path):
    stats = counters()
    guard = CameraPacketGuard(tmp_path / "diagnostics.jsonl", stats)
    guard.last_good_ns = 0
    bad = np.array([255, 216, 0, 0], np.uint8)
    good = np.array([255, 216, 255, 217, 0, 0], np.uint8)
    assert guard.check(bad, 100, 1_000_000_000, 1) is None
    np.testing.assert_array_equal(guard.check(good, 200, 1_500_000_000), good[:4])
    assert guard.check(bad, 300, 3_000_000_000, 2) is None
    with pytest.raises(RuntimeError, match="two seconds"):
        guard.check(bad, 400, 3_500_000_000, 2)
    assert stats["camera_rejected"].value == 3
    assert stats["record_camera_rejected"].value == 3
    destination = tmp_path / "camera_rejections.jsonl"
    save_recording_rejections(guard.path, destination, 2)
    rows = [json.loads(line) for line in destination.read_text().splitlines()]
    assert [row["timestamp_ns"] for row in rows] == [300, 400]
    assert all(row["reason"] == "missing_jpeg_end" for row in rows)


@pytest.mark.parametrize("scenario", ["normal", "grayscale", "start_first", "start_later"])
def test_capture_skips_bad_packets_before_and_after_initialization(
    tmp_path, monkeypatch, scenario
):
    from mx_eye import pipeline

    config = cfg.MxEyeConfigStore.defaults()
    config.value.source.mode = cfg.SourceMode.CAMERA
    config.value.source.width = config.value.source.height = 48
    config.value.source.fourcc = "MJPG"
    good = cv2.imencode(".jpg", np.zeros((48, 48, 3), np.uint8))[1].reshape(-1)
    buffers = [np.array([], np.uint8), good, good[:-2], good]
    if scenario == "grayscale":
        buffers = [np.zeros((48, 48), np.uint8)] * 2
    stop, done, recording = threading.Event(), threading.Event(), threading.Event()
    recording.set()
    recording_generation = SimpleNamespace(value=3 if scenario == "start_later" else 0 if scenario == "start_first" else 1)
    if scenario.startswith("start_"):
        recording.clear()
    reads = 0
    cap = Mock()
    cap.set.return_value = cap.isOpened.return_value = True
    cap.get.side_effect = lambda prop: (
        cv2.VideoWriter_fourcc(*"MJPG") if prop == cv2.CAP_PROP_FOURCC else 120
    )

    def read():
        nonlocal reads
        reads += 1
        if scenario.startswith("start_") and reads == 3:
            recording_generation.value += 1
            recording.set()
        frame = buffers.pop(0)
        if not buffers:
            stop.set()
        return True, frame

    cap.read.side_effect = read
    monkeypatch.setattr(pipeline.cv2, "VideoCapture", lambda *args: cap)
    monkeypatch.setattr(pipeline, "backend_for", lambda settings: cv2.CAP_V4L2)
    monkeypatch.setattr(pipeline, "apply_controls", lambda *args: {})
    monkeypatch.setattr(pipeline, "priority", lambda role: "test")
    monkeypatch.setattr(pipeline.signal, "signal", lambda *args: None)
    ticks = iter(
        [10_000_000_000, 10_010_000_000, 10_020_000_000, 10_030_000_000, 10_040_000_000]
    )
    monkeypatch.setattr(pipeline.time, "monotonic_ns", lambda: next(ticks))
    mailbox = Mock(pixels=bytearray(48 * 48 * 3))
    mailbox.put.return_value = True
    ring = Mock()
    ring.put.return_value = True
    stats = counters()
    path = tmp_path / "diagnostics.jsonl"
    capture_worker(
        config,
        mailbox,
        ring,
        stop,
        done,
        threading.Event(),
        queue.Queue(),
        SimpleNamespace(value=0),
        stats,
        queue.Queue(),
        recording=recording,
        recording_generation=recording_generation,
        camera_diagnostics_path=path,
    )
    assert done.is_set()
    assert stats["capture_fault"].value == 0
    if scenario == "grayscale":
        assert stats["camera_rejected"].value == 0
        assert not path.exists()
        assert mailbox.put.call_count == ring.put.call_count == 1
        assert ring.put.call_args.args[0].shape == (48, 48, 3)
        cap.set.assert_any_call(cv2.CAP_PROP_CONVERT_RGB, 1)
        return
    if scenario.startswith("start_"):
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        assert [row["recording_generation"] for row in rows] == [0, recording_generation.value]
        assert stats["record_camera_rejected"].value == 1
        assert ring.put.call_count == 1
        return
    assert stats["camera_rejected"].value == stats["record_camera_rejected"].value == 2
    assert mailbox.put.call_count == ring.put.call_count == 2
    metadata = [call.args[1] for call in ring.put.call_args_list]
    assert [row[3] for row in metadata] == [10_020_000_000, 10_040_000_000]
    assert stats["record_acquired"].value == stats["enqueued"].value == 2
    assert len(path.read_text().splitlines()) == 2
    cap.release.assert_called_once()


def test_recording_saves_its_rejection_counts_and_rows(tmp_path, monkeypatch):
    from pathlib import Path

    from mx_eye.service import Service
    from test_gui_workflow import free_port, wait_until

    monkeypatch.setenv("MX_EYE_DATA_DIR", str(tmp_path / "runtime"))
    config = cfg.MxEyeConfigStore.defaults()
    config.value.source.mode = cfg.SourceMode.SIMULATION
    config.value.source.fps = 15
    config.value.recording.directory = str(tmp_path / "recordings")
    config.value.network.data_port = free_port()
    config.value.network.control_port = free_port()
    service = Service(config)
    try:
        service.submit("start").result(timeout=15)
        for generation, rejected in ((1, 2), (2, 0)):
            service.submit("record", enabled=True).result(timeout=15)
            folder = Path(service.directory)
            wait_until(lambda: service.snapshot().stats.written >= 3)
            if rejected:
                # Inject capture-owned diagnostics; the real JPEG capture path
                # is exercised separately above without physical hardware.
                guard = CameraPacketGuard(
                    service.run["camera_diagnostics_path"], service.run["stats"]
                )
                now = time.monotonic_ns()
                for _ in range(rejected):
                    guard.check(np.array([], np.uint8), 100, now, generation)
            service.submit("record", enabled=False).result(timeout=3)
            wait_until(
                lambda: (
                    service.run["writer_done"].is_set()
                    and not service.run["processes"]["writer"].is_alive()
                )
            )
            summary = json.loads((folder / "session.json").read_text())
            assert summary["complete"], summary
            assert summary["camera_rejected"] == rejected
            assert summary["all_frames_saved"] == (not rejected)
            assert summary["recording_result"] == (
                "completed_with_skips" if rejected else "completed"
            )
            assert (
                len((folder / "camera_rejections.jsonl").read_text().splitlines())
                == rejected
            )
        assert service.run["stats"]["camera_rejected"].value == 2
    finally:
        service.close()
