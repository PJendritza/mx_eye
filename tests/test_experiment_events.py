import json
import time
import os
import subprocess
from datetime import datetime
import re
from pathlib import Path

from mx_eye import config as cfg
from mx_eye.service import Service
from py_mx_eye import ExperimentEvent, MxEye, MxEyeConfig
from test_sdk_read import _free_port


def test_recording_names_reserve_same_second_without_overwrite(tmp_path):
    from mx_eye.recording import reserve_recording_directory

    now = datetime(2026, 11, 2, 14, 30, 15)
    first = reserve_recording_directory(tmp_path, now)
    second = reserve_recording_directory(tmp_path, now)
    assert first.name == "2026-11-02_14-30-15"
    assert second.name == "2026-11-02_14-30-15_01"
    assert first.is_dir() and second.is_dir()


def test_pc_demo_with_real_game_loop(tmp_path, monkeypatch):
    import pytest

    framework = Path(__file__).resolve().parents[2] / "mxbiflow"
    python = framework / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        pytest.skip("Requires the sibling MXBI Flow PC development environment")
    monkeypatch.setenv("MX_EYE_DATA_DIR", str(tmp_path / "runtime"))
    config = cfg.MxEyeConfigStore.defaults()
    config.value.source.mode = cfg.SourceMode.SIMULATION
    config.value.source.fps = 15
    config.value.recording.directory = str(tmp_path / "recordings")
    config.value.network.data_port = _free_port()
    config.value.network.control_port = _free_port()
    service = Service(config)
    try:
        service.submit("start").result(timeout=15)
        env = dict(os.environ, SDL_VIDEODRIVER="dummy", SDL_AUDIODRIVER="dummy")
        result = subprocess.run([str(python), str(framework / "examples" / "eye_events_demo.py"),
            "--smoke", "--control-port", str(config.value.network.control_port),
            "--data-port", str(config.value.network.data_port)],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        logs = list((tmp_path / "eye_events_demo_data" / "data" / "eye_events").glob("*.jsonl"))
        assert len(logs) == 1
        rows = [json.loads(row) for row in logs[0].read_text().splitlines()]
        assert all(row["acknowledged"] for row in rows), rows
        kinds = [row["event"]["kind"] for row in rows]
        for kind in ("animal_entered", "animal_identified", "stimulus_shown",
                     "stimulus_changed", "animal_left", "run_ended"):
            assert kind in kinds, kinds
        assert not service.snapshot().recording
    finally:
        service.close()


def test_visit_recording_events_and_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("MX_EYE_DATA_DIR", str(tmp_path / "runtime"))
    config = cfg.MxEyeConfigStore.defaults()
    config.value.source.mode = cfg.SourceMode.SIMULATION
    config.value.source.fps = 15
    config.value.recording.directory = str(tmp_path / "recordings")
    config.value.network.data_port = _free_port()
    config.value.network.control_port = _free_port()
    service = Service(config)
    eye = MxEye(MxEyeConfig(data_port=config.value.network.data_port,
                            control_port=config.value.network.control_port))
    def event(kind, **kwargs):
        return ExperimentEvent(kind=kind, timestamp_ns=time.time_ns(),
            run_id="run-1", visit_id="visit-1", control_recording=True, **kwargs)
    try:
        eye.start()
        entry = event("animal_entered", animal="monkey", rfid_id="tag-1")
        started = eye.send_event(entry)
        assert started.recording
        directory = Path(started.directory)
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:_\d+)?", directory.name)
        assert eye.send_event(entry).directory == str(directory)
        eye.send_event(event("animal_identified", animal="identified", rfid_id="tag-1"))
        eye.send_event(event("stimulus_shown", stimulus_id="target", position=(200, 300)))
        time.sleep(0.3)
        stopped = eye.send_event(event("animal_left"))
        assert not stopped.recording
        deadline = time.monotonic() + 10
        while eye.status().recording_finalizing:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert eye.status().state == "running"
        assert eye.status().stats.written > 0
        assert eye.status().stats.recorded_seconds > 0
        rows = [json.loads(row) for row in (directory / "events.jsonl").read_text().splitlines()]
        assert [row["event"]["kind"] for row in rows] == [
            "animal_entered", "animal_identified", "stimulus_shown", "animal_left"]
        assert json.loads((directory / "experiment.json").read_text())["visit_id"] == "visit-1"
        assert (directory / "tracking.csv").exists()
        assert json.loads((directory / "session.json").read_text())["complete"]
    finally:
        eye.close()
        service.close()
