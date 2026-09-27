"""Runnable package entry point for the tracker: python -m mx_eye."""

import argparse
import multiprocessing as mp
import signal
import sys
from pathlib import Path

from PySide6 import QtWidgets as W

from . import config as cfg
from .config import SourceMode
from .gui import Window
from .widgets import STYLE


def main():
    mp.freeze_support()
    parser = argparse.ArgumentParser(description="mx_eye tracker")
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--demo", action="store_true", help="Use the artificial eye source"
    )
    args = parser.parse_args()
    config = cfg.configure(args.config)
    if args.demo:
        config.value.source.mode = SourceMode.SIMULATION
    app = W.QApplication.instance() or W.QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = Window()
    window.show()
    signal.signal(signal.SIGINT, lambda *_: window.close())
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
