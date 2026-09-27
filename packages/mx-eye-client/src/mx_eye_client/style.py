"""Application theme and label helper for the remote client.

Copied from mx_eye/widgets.py so the client does not depend on the tracker
package; the tracker keeps its own copy.
"""

from PySide6 import QtWidgets as W

STYLE = """
QWidget { background:#10151d; color:#dce6f1; font-family:Inter,Segoe UI,sans-serif; font-size:12px; }
QMainWindow,QDialog { background:#10151d; }
QLabel#brand { font-size:23px; font-weight:700; color:#f2f7ff; }
QLabel#muted { color:#8e9caf; }
QLabel#metric { padding:8px 13px; background:#192331; border-radius:6px; }
QPushButton,QToolButton { background:#233145; border:1px solid #34455b; border-radius:4px; padding:4px 7px; }
QPushButton:hover,QToolButton:hover { background:#30445e; }
QPushButton:disabled { color:#596a80; background:#18212d; }
QPushButton#primary { background:#267d68; border-color:#389982; font-weight:600; }
QLineEdit,QSpinBox,QDoubleSpinBox,QComboBox { background:#182330; border:1px solid #34455b; border-radius:3px; padding:2px; }
QScrollArea { border:0; }
QSlider::groove:horizontal { background:#2a3b50; height:4px; border-radius:2px; }
QSlider::handle:horizontal { background:#61b6ef; width:12px; margin:-5px 0; border-radius:6px; }
QTabBar::tab { background:#182330; padding:8px 15px; }
QTabBar::tab:selected { background:#2b405b; }
QCheckBox { spacing:7px; }
QSplitter::handle { background:#253345; }
QToolTip { background:#233145; color:#eaf3ff; border:1px solid #536b89; }
"""


def label(text, name=None):
    w = W.QLabel(text)
    if name:
        w.setObjectName(name)
    return w
