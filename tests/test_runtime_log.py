import json
import logging
import multiprocessing as mp
import os
import signal
import sys
import threading

import pytest

from mx_eye import runtime_log
from mx_eye.service import describe_exitcode


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.delenv(runtime_log.ENV_DIR, raising=False)
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    hooks = sys.excepthook, threading.excepthook
    signals = {
        n: signal.getsignal(n)
        for n in (signal.SIGTERM, getattr(signal, "SIGHUP", None), getattr(signal, "SIGQUIT", None))
        if n is not None
    }
    yield monkeypatch
    for handler in root.handlers[:]:
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)
    sys.excepthook, threading.excepthook = hooks
    for number, handler in signals.items():
        signal.signal(number, handler)
    runtime_log._directory = None
    os.environ.pop(runtime_log.ENV_DIR, None)


def _close_handlers():
    for handler in logging.getLogger().handlers[:]:
        if isinstance(handler, runtime_log._DurableFileHandler):
            logging.getLogger().removeHandler(handler)
            handler.close()


def _worker(path):
    os.environ[runtime_log.ENV_DIR] = path
    runtime_log.attach_worker("tracking")
    logging.getLogger("mx_eye.pipeline").error("worker failure marker")


def test_launch_folder_status_and_clean_exit(isolated, tmp_path):
    folder = runtime_log.start(tmp_path)
    assert folder is not None and folder.parent == tmp_path / "logs"
    assert os.environ[runtime_log.ENV_DIR] == str(folder)
    logging.getLogger("mx_eye.service").error("session failure marker")
    status = json.loads((folder / "status.json").read_text())
    assert status["pid"] == os.getpid() and "clean_exit" not in status
    runtime_log.finish(clean=True)
    status = json.loads((folder / "status.json").read_text())
    assert status["clean_exit"] is True
    text = (folder / "mx_eye.log").read_text()
    assert "session failure marker" in text and "mx_eye exiting (clean)" in text
    assert (folder / "faults.log").exists()


@pytest.mark.parametrize(
    "boot, expected",
    [("same", "process ended without closing"), ("other", "system restarted")],
)
def test_abrupt_previous_launch_is_classified(isolated, tmp_path, boot, expected):
    previous = tmp_path / "logs" / "20000101-000000-pid1"
    previous.mkdir(parents=True)
    current_boot = "boot-a"
    isolated.setattr(runtime_log, "boot_id", lambda: current_boot)
    (previous / "status.json").write_text(
        json.dumps({"pid": 1, "boot_id": current_boot if boot == "same" else "boot-b"})
    )
    (previous / "mx_eye.log").write_text(
        "x INFO health cpu_temp=80.0C throttled=0x50005\nx INFO last words\n"
    )
    folder = runtime_log.start(tmp_path)
    _close_handlers()
    text = (folder / "mx_eye.log").read_text()
    assert "did NOT close normally" in text and expected in text
    assert "last words" in text and "throttled=0x50005" in text


def test_old_launch_folders_are_pruned(isolated, tmp_path):
    for i in range(5):
        old = tmp_path / "logs" / f"2000010{i}-000000-pid{i}"
        old.mkdir(parents=True)
        (old / "status.json").write_text(json.dumps({"clean_exit": True}))
    folder = runtime_log.start(tmp_path, keep=3)
    _close_handlers()
    remaining = sorted(p.name for p in (tmp_path / "logs").iterdir())
    assert remaining == ["20000103-000000-pid3", "20000104-000000-pid4", folder.name]


def test_spawned_worker_appends_to_launch_log(isolated, tmp_path):
    folder = runtime_log.start(tmp_path)
    process = mp.get_context("spawn").Process(target=_worker, args=(str(folder),))
    process.start()
    process.join(30)
    assert process.exitcode == 0
    _close_handlers()
    text = (folder / "mx_eye.log").read_text()
    assert "tracking worker started" in text and "worker failure marker" in text


def test_health_sample_and_throttle_description(isolated, tmp_path):
    runtime_log.start(tmp_path)
    isolated.setattr(runtime_log, "throttled_flags", lambda: 0x50005)
    line = runtime_log.HealthMonitor(state=lambda: "running", disk=tmp_path / "missing").sample()
    assert "throttled=0x50005" in line and "state=running" in line and "disk_free=" in line
    _close_handlers()
    text = (runtime_log.directory() / "mx_eye.log").read_text()
    assert "under-voltage now" in text
    assert runtime_log.describe_throttled(0) == "ok"
    assert "under-voltage has occurred" in runtime_log.describe_throttled(1 << 16)


def test_exit_codes_are_readable():
    assert describe_exitcode(None) == "still running"
    assert describe_exitcode(1) == "exit code 1"
    assert "SIGKILL" in describe_exitcode(-9) and "out-of-memory" in describe_exitcode(-9)
