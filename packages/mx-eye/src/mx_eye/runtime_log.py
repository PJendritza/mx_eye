"""Per-launch runtime log for diagnosing crashes, kills and power loss.

Every launch of the tracker gets its own folder under
``<data root>/logs/<YYYYmmdd-HHMMSS>-pid<pid>/``:

``mx_eye.log``
    Log lines from the GUI/supervisor process and every worker process
    (acquisition, tracking, recording), including full tracebacks, worker
    exit codes, received termination signals and a periodic ``health`` line
    (CPU temperature, Raspberry Pi under-voltage/throttling flags, memory,
    load, disk space, tracker state).
``faults.log``
    Native crash dumps (segmentation faults, aborts in OpenCV/FFmpeg/Qt)
    written by :mod:`faulthandler`, one section per process.
``status.json``
    Process ID, boot ID and start time; ``clean_exit`` is added only when the
    application closes normally.

Warnings, errors and health lines are flushed to the storage device with
``fsync`` so that the last lines survive an abrupt power loss. On the next
launch, a previous folder without ``clean_exit`` is reported in the new log,
classified as a system restart/power loss (boot ID changed) or as the process
having been killed while the system kept running (same boot ID).
"""

from __future__ import annotations

import datetime as dt
import faulthandler
import json
import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

ENV_DIR = "MX_EYE_RUNTIME_LOG_DIR"
LOG_NAME = "mx_eye.log"
FAULT_NAME = "faults.log"
STATUS_NAME = "status.json"
KEEP_LAUNCHES = 50
HEALTH_INTERVAL_S = 5.0

logger = logging.getLogger("mx_eye")

# Raspberry Pi firmware get_throttled bits.
THROTTLE_BITS = {
    0: "under-voltage now",
    1: "ARM frequency capped now",
    2: "throttled now",
    3: "soft temperature limit now",
    16: "under-voltage has occurred",
    17: "ARM frequency capping has occurred",
    18: "throttling has occurred",
    19: "soft temperature limit has occurred",
}
_THROTTLE_SYSFS = Path("/sys/devices/platform/soc/soc:firmware/get_throttled")
_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s [%(processName)s pid=%(process)d] %(name)s: %(message)s"

_directory: Path | None = None
_fault_file = None


class _DurableFileHandler(logging.FileHandler):
    """Append handler that fsyncs warnings/errors so they survive power loss."""

    def emit(self, record):
        super().emit(record)
        if record.levelno >= logging.WARNING or getattr(record, "durable", False):
            try:
                if self.stream is not None:
                    os.fsync(self.stream.fileno())
            except (OSError, ValueError):
                pass


def directory() -> Path | None:
    """The folder of the current launch, or ``None`` when logging is inactive."""
    return _directory


def _attach(path: Path, role: str) -> None:
    global _fault_file
    root = logging.getLogger()
    if any(
        isinstance(h, _DurableFileHandler) and Path(h.baseFilename) == (path / LOG_NAME).resolve()
        for h in root.handlers
    ):
        return
    handler = _DurableFileHandler(path / LOG_NAME, mode="a", encoding="utf-8")
    handler.setFormatter(logging.Formatter(_FORMAT, "%Y-%m-%d %H:%M:%S"))
    root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    # Keep the handle open for the lifetime of the process: faulthandler writes
    # to the raw file descriptor from a signal handler.
    _fault_file = (path / FAULT_NAME).open("a", encoding="utf-8")
    _fault_file.write(
        f"--- {role} pid={os.getpid()} started {dt.datetime.now().isoformat(timespec='seconds')}\n"
    )
    _fault_file.flush()
    faulthandler.enable(_fault_file, all_threads=True)

    def excepthook(kind, value, tb):
        logger.critical("Uncaught exception in %s", role, exc_info=(kind, value, tb))
        sys.__excepthook__(kind, value, tb)

    def thread_excepthook(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "?"
        logger.critical(
            "Uncaught exception in thread %s",
            name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook


def start(root: Path, keep: int = KEEP_LAUNCHES) -> Path | None:
    """Create this launch's log folder and install hooks in the main process.

    Returns ``None`` (and logs nothing) when the folder cannot be created, so
    diagnostics never prevent the tracker from starting.
    """
    global _directory
    base = Path(root) / "logs"
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = base / f"{stamp}-pid{os.getpid()}"
    try:
        path.mkdir(parents=True, exist_ok=True)
        previous = _previous_launches(base, path)
        _write_json(
            path / STATUS_NAME,
            dict(
                pid=os.getpid(),
                boot_id=boot_id(),
                started=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            ),
        )
        _attach(path, "main")
    except OSError as exc:
        print(f"mx_eye: runtime log unavailable: {exc}", file=sys.stderr)
        return None
    _directory = path
    os.environ[ENV_DIR] = str(path)
    logger.info("mx_eye started; runtime log folder %s", path)
    logger.info(
        "Python %s on %s; boot ID %s; system up since %s",
        platform.python_version(),
        platform.platform(),
        boot_id(),
        _boot_time(),
    )
    model = _read_text(Path("/proc/device-tree/model"))
    if model:
        logger.info("Hardware: %s", model.rstrip("\x00"))
    for previous_path in previous[:1]:
        _report_previous(previous_path)
    for old in previous[max(keep - 1, 0):]:
        shutil.rmtree(old, ignore_errors=True)
    _install_signal_logging()
    return path


def attach_worker(role: str) -> None:
    """Join the launch log from a spawned worker process (no-op if inactive)."""
    value = os.environ.get(ENV_DIR)
    if not value:
        return
    try:
        _attach(Path(value), role)
    except OSError:
        return
    logger.info("%s worker started", role)


def finish(clean: bool = True) -> None:
    """Mark this launch as closed normally; an absent marker means abrupt end."""
    if _directory is None:
        return
    logger.info("mx_eye exiting (%s)", "clean" if clean else "with error")
    try:
        status = json.loads((_directory / STATUS_NAME).read_text(encoding="utf-8"))
        status.update(
            clean_exit=clean,
            exited=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        _write_json(_directory / STATUS_NAME, status)
    except (OSError, ValueError):
        pass
    logging.shutdown()


def boot_id() -> str:
    return _read_text(Path("/proc/sys/kernel/random/boot_id")).strip()


def _boot_time() -> str:
    try:
        import psutil

        return dt.datetime.fromtimestamp(psutil.boot_time()).isoformat(timespec="seconds")
    except Exception:
        return "unknown"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def _previous_launches(base: Path, current: Path) -> list[Path]:
    """Earlier launch folders, newest first."""
    try:
        folders = [p for p in base.iterdir() if p.is_dir() and p != current]
    except OSError:
        return []
    return sorted(folders, key=lambda p: p.name, reverse=True)


def _last_lines(path: Path, count: int) -> list[str]:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 64 * 1024))
            data = stream.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return [line for line in data.splitlines() if line.strip()][-count:]


def _report_previous(path: Path) -> None:
    try:
        status = json.loads((path / STATUS_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = {}
    if status.get("clean_exit"):
        logger.info("Previous launch %s closed normally.", path.name)
        return
    lines = _last_lines(path / LOG_NAME, 40)
    last = lines[-1] if lines else "(no log lines)"
    health = next((line for line in reversed(lines) if " health " in line), None)
    previous_boot = status.get("boot_id")
    current_boot = boot_id()
    if previous_boot and current_boot and previous_boot != current_boot:
        cause = (
            "the system restarted while mx_eye was running (power loss, crash, "
            "watchdog or a reboot). Check 'journalctl -b -1' for the previous boot"
        )
    elif previous_boot and previous_boot == current_boot:
        cause = (
            "the process ended without closing while the system kept running "
            "(killed, e.g. by the out-of-memory killer, the desktop/VNC session "
            "ending, or a native crash; see faults.log and 'journalctl -k')"
        )
    else:
        cause = "the cause cannot be determined from the boot ID"
    logger.warning(
        "Previous launch %s did NOT close normally: %s. Its last log line: %s",
        path.name,
        cause,
        last,
    )
    if health and health != last:
        logger.warning("Previous launch's last health line: %s", health)
    faults = _read_text(path / FAULT_NAME)
    if "Fatal Python error" in faults or "Traceback" in faults:
        logger.warning("Previous launch recorded a native crash; see %s", path / FAULT_NAME)


def _install_signal_logging() -> None:
    """Record termination signals, then keep their default effect."""
    names = ["SIGTERM", "SIGHUP", "SIGQUIT"]
    for name in names:
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            previous = signal.getsignal(number)
        except (ValueError, OSError):
            continue

        def handler(signum, frame, previous=previous, name=name):
            logger.critical(
                "Received %s; mx_eye is being terminated by the system or session "
                "(for example a logout, the desktop/VNC session closing, or shutdown).",
                name,
            )
            if callable(previous):
                previous(signum, frame)
                return
            if previous == signal.SIG_IGN:
                return
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        try:
            signal.signal(number, handler)
        except (ValueError, OSError):
            pass


# ---------------------------------------------------------------- health


def throttled_flags() -> int | None:
    """Raspberry Pi firmware throttle/under-voltage bitmask, if available."""
    text = _read_text(_THROTTLE_SYSFS).strip()
    if text:
        try:
            return int(text, 16)
        except ValueError:
            pass
    tool = shutil.which("vcgencmd")
    if tool is None:
        return None
    try:
        out = subprocess.run(
            [tool, "get_throttled"], capture_output=True, text=True, timeout=2, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"0x[0-9a-fA-F]+", out)
    return int(match.group(0), 16) if match else None


def describe_throttled(value: int) -> str:
    names = [text for bit, text in THROTTLE_BITS.items() if value & (1 << bit)]
    return ", ".join(names) if names else "ok"


def _input_voltage() -> float | None:
    """Raspberry Pi 5 USB-C input voltage (PMIC), if the firmware reports it."""
    tool = shutil.which("vcgencmd")
    if tool is None:
        return None
    try:
        out = subprocess.run(
            [tool, "pmic_read_adc", "EXT5V_V"], capture_output=True, text=True, timeout=2, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"=\s*([0-9.]+)V", out)
    return float(match.group(1)) if match else None


def _cpu_temperature() -> float | None:
    text = _read_text(Path("/sys/class/thermal/thermal_zone0/temp")).strip()
    try:
        return int(text) / 1000
    except ValueError:
        pass
    try:
        import psutil

        temps = psutil.sensors_temperatures()  # type: ignore[attr-defined]
        for entries in temps.values():
            if entries:
                return float(entries[0].current)
    except Exception:
        pass
    return None


class HealthMonitor:
    """Background thread writing a durable health line at a fixed interval."""

    def __init__(
        self,
        state: Callable[[], str] = lambda: "",
        interval: float = HEALTH_INTERVAL_S,
        disk: Path | None = None,
    ):
        self.state = state
        self.interval = interval
        self.disk = disk
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="mx-eye health"
        )
        self._last_throttled: int | None = None
        self._voltage_supported = True

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join(2)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.sample()
            except Exception:
                logger.exception("Health sample failed")
            self._stop.wait(self.interval)

    def sample(self) -> str:
        import psutil

        parts = []
        temp = _cpu_temperature()
        if temp is not None:
            parts.append(f"cpu_temp={temp:.1f}C")
        throttled = throttled_flags()
        if throttled is not None:
            parts.append(f"throttled=0x{throttled:x}")
            if throttled != self._last_throttled:
                level = logging.WARNING if throttled & 0xF else logging.INFO
                if self._last_throttled is not None or throttled:
                    logger.log(
                        level,
                        "Raspberry Pi power/thermal flags changed to 0x%x: %s",
                        throttled,
                        describe_throttled(throttled),
                    )
                self._last_throttled = throttled
        if self._voltage_supported:
            volts = _input_voltage()
            if volts is None:
                self._voltage_supported = False
            else:
                parts.append(f"input={volts:.2f}V")
        parts.append(f"cpu={psutil.cpu_percent():.0f}%")
        try:
            load = os.getloadavg()
            parts.append(f"load={load[0]:.2f}")
        except (AttributeError, OSError):
            pass
        memory = psutil.virtual_memory()
        parts.append(f"mem_avail={memory.available / 2**20:.0f}MiB({100 - memory.percent:.0f}%)")
        swap = psutil.swap_memory()
        if swap.total:
            parts.append(f"swap_used={swap.used / 2**20:.0f}MiB")
        try:
            process = psutil.Process()
            rss = process.memory_info().rss
            for child in process.children(recursive=True):
                try:
                    rss += child.memory_info().rss
                except psutil.Error:
                    pass
            parts.append(f"mx_eye_rss={rss / 2**20:.0f}MiB")
        except psutil.Error:
            pass
        if self.disk is not None:
            target = Path(self.disk).absolute()
            while not target.exists() and target.parent != target:
                target = target.parent
            try:
                parts.append(f"disk_free={shutil.disk_usage(target).free / 2**30:.1f}GiB")
            except OSError:
                pass
        try:
            state = self.state()
        except Exception:
            state = "?"
        if state:
            parts.append(f"state={state}")
        line = " ".join(parts)
        logger.info("health %s", line, extra={"durable": True})
        return line
