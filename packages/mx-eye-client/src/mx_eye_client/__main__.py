"""Runnable package entry point for the remote client: python -m mx_eye_client."""

import argparse
import sys

from PySide6 import QtWidgets as W

from .receiver import Receiver
from .style import STYLE


def main():
    parser = argparse.ArgumentParser(description="mx_eye SDK receiver demonstration")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--data-port", type=int, default=5556)
    parser.add_argument("--control-port", type=int, default=5557)
    args = parser.parse_args()
    app = W.QApplication.instance() or W.QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = Receiver(args.host, args.data_port, args.control_port)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
