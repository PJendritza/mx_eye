"""End-to-end simulation: SDK REQ commands and SUB samples from worker processes."""

import time

import pytest
from mx_eye import config as cfg
from mx_eye.service import Service
from mx_eye_protocol import TrackingFlags
from py_mx_eye import MxEye, MxEyeConfig
from test_sdk_read import _free_port


def test_simulation_control_data_and_restart(tmp_path):
    config = cfg.MxEyeConfigStore.defaults()
    config.value.source.mode = cfg.SourceMode.SIMULATION
    config.value.source.fps = 30
    config.value.recording.directory = str(tmp_path)
    config.value.network.data_port = _free_port()
    config.value.network.control_port = _free_port()
    service = Service(config)
    eye = MxEye(
        MxEyeConfig(
            data_port=config.value.network.data_port,
            control_port=config.value.network.control_port,
        )
    )
    try:
        eye.connect()
        assert eye.status().state == "idle"
        assert list(eye.read(timeout=0.02)) == []
        sessions = []
        for _ in range(2):
            status = eye.start()
            assert status.state == "running"
            sample = next(eye.read(timeout=3, max_age_ms=None, require_valid=False))
            assert sample.frame.frame_size == 97
            assert sample.frame.flags & TrackingFlags.SIMULATION
            assert sample.frame.session == status.session
            sessions.append(status.session)
            assert eye.stop().state in {"stopping", "idle"}
            deadline = time.monotonic() + 10
            while eye.status().state != "idle":
                if time.monotonic() >= deadline:
                    pytest.fail("Simulation did not stop")
                time.sleep(0.02)
            # Drain samples from the stopped session before restarting.
            list(eye.read(timeout=0.05, max_age_ms=None, require_valid=False))
        assert sessions[0] != sessions[1]
        assert not list(tmp_path.iterdir())
    finally:
        eye.close()
        service.close()


def test_receiver_gui_owns_subscriber_on_gui_thread(monkeypatch):
    import os
    import threading

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from mx_eye_client.receiver import Receiver
    from py_mx_eye.receiver import SampleReceiver
    from PySide6 import QtWidgets as W

    threads = []
    original = SampleReceiver.open

    def record_open(receiver):
        threads.append(threading.get_ident())
        return original(receiver)

    monkeypatch.setattr(SampleReceiver, "open", record_open)
    app = W.QApplication.instance() or W.QApplication([])
    window = Receiver("127.0.0.1", _free_port(), _free_port())
    try:
        window.timer.stop()
        window.connect_tracker()
        assert window.connected
        assert threads == [threading.get_ident()]
        assert window.pending is None
        assert list(window.client.read(timeout=0)) == []
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()
