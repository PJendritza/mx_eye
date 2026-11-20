"""Runnable package entry point for the tracker: python -m mx_eye."""

import argparse
import logging
import multiprocessing as mp
import signal
import sys
from pathlib import Path

from PySide6 import QtCore as C
from PySide6 import QtWidgets as W

from . import config as cfg
from . import runtime_log
from .config import SourceMode
from .gui import Window
from .paths import config_path, data_root, set_data_root
from .widgets import STYLE


def main():
    mp.freeze_support()
    parser = argparse.ArgumentParser(description="mx_eye tracker")
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Directory for persistent settings and animal calibrations",
    )
    parser.add_argument(
        "--demo", action="store_true", help="Use the artificial eye source"
    )
    args = parser.parse_args()
    set_data_root(args.data_dir)
    runtime_log.start(data_root())
    try:
        code = _run(args)
    except BaseException:
        runtime_log.logger.critical("mx_eye stopped by an exception", exc_info=True)
        runtime_log.finish(clean=False)
        raise
    runtime_log.finish(clean=code == 0)
    return code


def _qt_message(mode, context, message):
    level = {
        C.QtMsgType.QtDebugMsg: logging.DEBUG,
        C.QtMsgType.QtInfoMsg: logging.INFO,
        C.QtMsgType.QtWarningMsg: logging.WARNING,
        C.QtMsgType.QtCriticalMsg: logging.ERROR,
        C.QtMsgType.QtFatalMsg: logging.CRITICAL,
    }.get(mode, logging.WARNING)
    logging.getLogger("mx_eye.qt").log(level, "%s", message)
    if level >= logging.WARNING:
        print(message, file=sys.stderr)


def _run(args):
    startup_config = args.config or config_path()
    config = cfg.configure(startup_config if startup_config.exists() else None)
    if config.path is None:
        config.path = startup_config
    if args.demo:
        config.value.source.mode = SourceMode.SIMULATION
    app = W.QApplication.instance() or W.QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    C.qInstallMessageHandler(_qt_message)
    window = Window()
    window.show()
    signal.signal(signal.SIGINT, lambda *_: window.close())
    health = runtime_log.HealthMonitor(
        state=lambda: window.service.state,
        disk=Path(config.value.recording.directory),
    )
    if runtime_log.directory() is not None:
        health.start()
    try:
        return app.exec()
    finally:
        health.stop()


if __name__ == "__main__":
    raise SystemExit(main())
