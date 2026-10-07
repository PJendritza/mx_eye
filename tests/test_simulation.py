"""Simulation geometry and real tracking without camera/network hardware."""

import numpy as np
import pytest

from mx_eye.simulation import Simulation
from mx_eye.tracking import Tracker


def test_gaze_moves_pupils_but_not_cr_or_face():
    model = Simulation(640, 480)
    tracker = Tracker()
    neutral = tracker.process(model.render((0, 0, 0, 0, 0), 0), 1)
    moved = tracker.process(model.render((0, 0, 3, 0, 0), 0), 2)
    assert neutral['valid'] and moved['valid']
    assert abs(moved['pupil']['x'] - neutral['pupil']['x'] - 3) < .2
    assert moved['cr']['x'] == neutral['cr']['x']
    assert np.array_equal(model.render((0, 0, 0, 0, 0), 0)[200:],
                          model.render((0, 0, 3, 0, 0), 0)[200:])


def test_template_follows_same_eye_and_blink_reacquires():
    model = Simulation(640, 480)
    tracker = Tracker()
    assert tracker.process(model.render((0, 0, 0, 0, 0), 0), 1)['valid']
    tracker.set_template(160, 120)
    for frame in range(2, 8):
        dx = frame - 1
        result = tracker.process(model.render((dx, 0, 0, 0, 0), 0), frame)
        assert result['template_center'] == (160 + dx, 120)
        assert result['template_score'] >= tracker.config.template_min_corr
    for frame in range(8, 14):
        result = tracker.process(model.render((6, 0, 0, 0, 1), .5), frame)
        assert not result['valid']
        assert result['pupil'] is None and result['cr'] is None
    for frame in range(14, 24):
        result = tracker.process(model.render((6, 0, 0, 0, 1), 1.1), frame)
    assert result['valid']
    assert result['template_center'] == (166, 120)


def test_eye_hit_testing_and_gaze_clamp():
    model = Simulation(640, 480)
    state = (20, 10, 0, 0, 0)
    assert model.eye_at((180, 130), state) == (180, 130)
    assert model.eye_at((380, 130), state) == (380, 130)
    assert model.eye_at((280, 200), state) is None
    dx, dy = model.gaze((1000, 1000), (180, 130))
    assert abs((dx / 27) ** 2 + (dy / 13) ** 2 - 1) < 1e-6


@pytest.mark.parametrize('crop', [False, True])
def test_simulation_gestures_keep_tracking_controls_available(monkeypatch, crop):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    from PySide6 import QtCore as C, QtTest, QtWidgets as W
    from mx_eye.widgets import EyeView

    app = W.QApplication.instance() or W.QApplication([])
    model = Simulation(640, 480)
    state = (0, 0, 0, 0, 0)
    view = EyeView(crop=crop)
    view.resize(640, 480)
    view.set_frame(model.render(state, 0), {})
    view.simulation_context = lambda: (model, state)
    actions = []
    view.action.connect(lambda command, args: actions.append((command, args)))
    try:
        view.show()
        app.processEvents()
        QtTest.QTest.mouseClick(view, C.Qt.LeftButton, pos=C.QPoint(163, 120))
        assert actions[-1] == ('simulation', {'gesture': 'gaze', 'position': (3, 0)})
        QtTest.QTest.mouseClick(view, C.Qt.RightButton, pos=C.QPoint(160, 120))
        assert actions[-1] == ('simulation', {'gesture': 'blink'})
        QtTest.QTest.mouseClick(view, C.Qt.RightButton, C.Qt.ControlModifier, C.QPoint(165, 116))
        if crop:
            assert actions[-1][0] == 'pick' and actions[-1][1]['kind'] == 'cr'
        else:
            assert actions[-1][0] == 'template'
        QtTest.QTest.mouseClick(view, C.Qt.LeftButton, C.Qt.ShiftModifier, C.QPoint(160, 120))
        assert actions[-1][0] == 'template'
        QtTest.QTest.mousePress(view, C.Qt.LeftButton, pos=C.QPoint(260, 210))
        QtTest.QTest.mouseRelease(view, C.Qt.LeftButton, pos=C.QPoint(280, 220))
        assert actions[-1] == ('simulation', {'gesture': 'head', 'position': (20, 10)})
        view.simulation_context = lambda: None  # Manipulation switched off.
        QtTest.QTest.mouseClick(view, C.Qt.RightButton, pos=C.QPoint(160, 120))
        assert actions[-1][0] == ('pick' if crop else 'template')
    finally:
        view.close()
        view.deleteLater()
        app.processEvents()


def test_ctrl_temporarily_unchecks_toggle_and_restores_user_choice(monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    from PySide6 import QtCore as C, QtGui as G, QtWidgets as W
    from mx_eye import config as cfg
    from mx_eye.gui import Window
    from mx_eye.service import Service

    config = cfg.MxEyeConfigStore.defaults()
    config.value.source.mode = cfg.SourceMode.SIMULATION
    monkeypatch.setattr(cfg, 'store', lambda: config)
    monkeypatch.setattr(Service, '_start_server', lambda self: None)
    monkeypatch.setattr(Window, 'isActiveWindow', lambda self: True)
    app = W.QApplication.instance() or W.QApplication([])
    window = Window()
    try:
        assert window.manipulate_simulation.isChecked()
        assert window.eye.simulation_context is not None
        for initial in (True, False):
            window.manipulate_simulation.setChecked(initial)
            window.eventFilter(window, G.QKeyEvent(C.QEvent.KeyPress, C.Qt.Key_Control, C.Qt.ControlModifier))
            assert not window.manipulate_simulation.isChecked()
            assert not window.manipulate_simulation.isEnabled()
            window.eventFilter(window, G.QKeyEvent(C.QEvent.KeyRelease, C.Qt.Key_Control, C.Qt.NoModifier))
            assert window.manipulate_simulation.isChecked() == initial
            assert window.manipulate_simulation.isEnabled()
        window.manipulate_simulation.setChecked(True)
        window.set_simulation_control_override(True)
        window.eventFilter(window, C.QEvent(C.QEvent.WindowDeactivate))
        assert window.manipulate_simulation.isChecked()
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()
