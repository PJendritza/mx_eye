"""Recording boundaries and GUI controls without camera hardware."""
import csv
import json
import os
import socket
import time
from pathlib import Path

import pytest

from mx_eye import config as cfg
from mx_eye.service import Service


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("Timed out waiting for recording / tracking state")
        time.sleep(0.01)


def test_recording_can_stop_and_restart_without_stopping_tracking(tmp_path):
    config = cfg.MxEyeConfigStore.defaults()
    config.value.source.mode = cfg.SourceMode.SIMULATION
    config.value.source.fps = 30
    config.value.recording.directory = str(tmp_path)
    config.value.recording.buffer_mb = 32
    config.value.network.control_port = free_port()
    config.value.network.data_port = free_port()
    service = Service(config)
    try:
        service.submit("start").result(timeout=15)
        wait_until(lambda: service.snapshot().stats.tracked > 3)
        assert service.directory == ""
        assert not list(tmp_path.iterdir())
        directories = []
        for _ in range(2):
            service.submit("record", enabled=True).result(timeout=15)
            directory = Path(service.directory)
            directories.append(directory)
            wait_until(lambda: service.snapshot().stats.written >= 5)
            service.submit("record", enabled=False).result(timeout=3)
            wait_until(lambda: service.run["writer_done"].is_set()
                       and not service.run["processes"]["writer"].is_alive())
            assert service.snapshot().state == "running"
            before = service.snapshot().stats.tracked
            wait_until(lambda: service.snapshot().stats.tracked > before + 2)
            with (directory / "tracking.csv").open(newline="") as stream:
                rows = list(csv.reader(stream))
            assert len(rows[0]) == 17
            assert len(rows) > 1
            assert all(len(row) == 17 for row in rows)
            summary = json.loads((directory / "session.json").read_text())
            assert summary["complete"], summary
            assert summary["acquired"] == summary["written"] >= 5
        assert directories[0] != directories[1]
        # A rapid click pair must still produce a reliable end-of-log boundary.
        service.submit("record", enabled=True).result(timeout=15)
        service.submit("record", enabled=False).result(timeout=3)
        wait_until(lambda: service.run["writer_done"].is_set()
                   and not service.run["processes"]["writer"].is_alive())
        # Stopping tracking while recording must also drain/finalize.
        service.submit("record", enabled=True).result(timeout=15)
        wait_until(lambda: service.snapshot().stats.written >= 3)
        service.submit("stop").result(timeout=3)
        wait_until(lambda: service.run is None)
        assert service.snapshot().state == "idle", service.snapshot()
    finally:
        service.close()


def test_gui_menus_modes_and_suspended_views(monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets as W
    from mx_eye.gui import Settings, Window

    app = W.QApplication.instance() or W.QApplication([])
    config = cfg.MxEyeConfigStore.defaults()
    config.value.network.control_port = free_port()
    config.value.network.data_port = free_port()
    monkeypatch.setattr(cfg, "store", lambda: config)
    window = Window()
    window.show()
    app.processEvents()
    try:
        assert [a.text() for a in window.menuBar().actions()] == [
            "File", "Settings", "Calibration", "Tools"
        ]
        assert window.reason_label.isVisible()
        assert window.playback_note.isVisible()
        geometry = (window.eye.geometry(), window.full.geometry(), window.size())
        window._playback_text = "Playback limited: 0.91× achieved / 1.00× requested"
        window.update_alert()
        app.processEvents()
        assert (window.eye.geometry(), window.full.geometry(), window.size()) == geometry
        window._playback_text = ""
        window.update_alert()
        app.processEvents()
        assert (window.eye.geometry(), window.full.geometry(), window.size()) == geometry
        window.service.source_info.update(mode=cfg.SourceMode.CAMERA, driver_fps=30)
        window.refresh()
        assert window.requested_fps.text() == "30 fps camera"
        assert window.start_button.isVisible()
        assert not window.open_button.isVisible()
        window.source.setCurrentIndex(window.source.findData(cfg.SourceMode.VIDEO.value))
        window.refresh()
        assert window.open_button.isVisible()
        assert not window.start_button.isVisible()
        assert not window.record_button.isEnabled()
        dialog = Settings(config, window)
        tabs = dialog.findChild(W.QTabWidget)
        assert [tabs.tabText(i) for i in range(tabs.count())] == [
            "Camera", "Network", "Recording", "View"
        ]
        dialog.reject()
        display = config.value.display.model_copy(update={"hz": 10})
        window.apply_view_settings(display)
        assert window.timer.interval() == 100
        window.display_pause.setChecked(True)
        window.refresh()
        assert not window.full.isEnabled()
        assert not window.eye.isEnabled()
        window.show_diagnostics()
        window.refresh()
        assert "ms" in window.diagnostic_values["GUI callback"].text()
        window.diagnostics.close()
        window.display_pause.setChecked(False)
        window.refresh()
        assert window.full.isEnabled()
        assert window.timer.interval() == 100
    finally:
        window.timer.stop()
        window.service.close()
        window.deleteLater()
        app.processEvents()
