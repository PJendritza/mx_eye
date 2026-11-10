"""Live commands, driver defaults and reproducible software brightness."""
import queue
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np
import pytest

from mx_eye import config as cfg
from mx_eye.camera_backend import apply_controls
from mx_eye.service import Service
from mx_eye.tracking import Tracker


def test_linux_gain_uses_advertised_default_and_explicit_gain(monkeypatch):
    import mx_eye.camera_backend as backend
    calls = []
    def v4l2(device, *args):
        calls.extend(args)
        return 'gain 0x00980913 (int) : min=0 max=63 step=1 default=34 value=0'
    monkeypatch.setattr(backend, 'v4l2', v4l2)
    monkeypatch.setattr(backend, 'backend_for', lambda source: cv2.CAP_V4L2)
    source = cfg.SourceConfig(backend='v4l2').model_dump(mode='json')
    apply_controls(None, source)
    assert '--set-ctrl=gain=34' in calls
    calls.clear()
    apply_controls(None, dict(source, gain=0, exposure_mode='manual', exposure_ms=8))
    assert '--set-ctrl=gain=0' in calls


def test_windows_uses_native_manual_flags_path_and_driver_default(monkeypatch):
    import mx_eye.camera_backend as backend
    monkeypatch.setattr(backend.sys, 'platform', 'win32')
    native = Mock()
    native.describe.return_value = {'gain': {'default': 34}}
    native.set.return_value = 'accepted'
    apply_controls(None, cfg.SourceConfig(exposure_mode='manual', exposure_ms=8).model_dump(mode='json'), native)
    native.set.assert_any_call('exposure', -7, auto=False)
    native.set.assert_any_call('gain', 34)
    native.close.assert_not_called()


def test_brightness_changes_processing_without_changing_acquired_frame():
    frame = np.full((48, 48, 3), 100, dtype=np.uint8)
    tracker = Tracker(cfg.TrackingConfig(image_brightness=3))
    tracker.process(frame, 1)
    assert np.all(frame == 100)
    assert np.all(tracker.current_frame == 255)
    tracker.config.image_brightness = .5
    tracker.process(frame, 1, advance=False)
    np.testing.assert_array_equal(tracker.current_frame, cv2.convertScaleAbs(frame, alpha=.5))


def test_live_command_changes_only_controls_and_queues_brightness():
    service = Service.__new__(Service)
    service.config = cfg.MxEyeConfigStore.defaults()
    service.run = {'capture_commands': queue.Queue(), 'commands': queue.Queue()}
    service.revision = 0
    service.snapshot = lambda: None
    controls = dict(exposure_mode='manual', exposure_ms=8, gain=None)
    service._execute('camera_controls', dict(controls=controls, image_brightness=1.5))
    assert service.run['capture_commands'].get_nowait()['controls'] == controls
    assert service.run['commands'].get_nowait()['tracking'].image_brightness == 1.5
    assert service.config.value.source.width == 640
    with pytest.raises(ValueError):
        service._execute('camera_controls', dict(controls={'width': 1280}, image_brightness=1))


def test_gain_default_and_live_mode_in_camera_form(monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    from PySide6 import QtWidgets as W
    from mx_eye.cameras import CameraControls, CameraModeDiscovery
    app = W.QApplication.instance() or W.QApplication([])
    discovery = CameraModeDiscovery()
    discovery.cameras = [dict(index=0, name='test', backend='v4l2', device='/dev/video0', error='',
        controls={'gain': dict(min=0, max=63, step=1, default=34)},
        formats=[dict(width=640, height=480, fourcc='MJPG', fps=120)])]
    widget = CameraControls(cfg.SourceConfig(), discovery=discovery)
    assert widget.gain.value() == 34
    assert 'default 34' in widget.gain_info.text()
    assert widget.values().gain is None
    widget.set_running(True)
    assert not widget.fps.isEnabled()
    assert widget.exposure_mode.isEnabled()
    assert widget.brightness.isEnabled()
    widget.stop_scan()
    widget.close()
    app.processEvents()


def test_settings_auto_apply_and_follow_start_stop(monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    from PySide6 import QtCore as C, QtTest, QtWidgets as W
    from mx_eye.cameras import CameraModeDiscovery
    from mx_eye.gui import Settings
    app = W.QApplication.instance() or W.QApplication([])
    parent = W.QWidget()
    parent.service = SimpleNamespace(config=cfg.MxEyeConfigStore.defaults(), run=None,
                                     camera_control_info='')
    parent.camera_discovery = CameraModeDiscovery(parent)
    parent.camera_discovery.cameras = []
    parent.camera = W.QSpinBox()
    parent.call = Mock()
    dialog = Settings(parent.service.config, parent)
    dialog.show()
    assert dialog.windowModality() == C.Qt.NonModal
    controls = dialog.camera_controls
    controls.exposure_mode.setCurrentIndex(controls.exposure_mode.findData('manual'))
    controls.exposure.setValue(8)
    controls.brightness.setValue(1.5)
    controls.gain_on.setChecked(True)
    controls.gain.setValue(34)
    QtTest.QTest.qWait(350)
    assert parent.call.call_count == 1
    parent.call.assert_called_with('camera_controls',
        controls=dict(exposure_mode='manual', exposure_ms=8, gain=34), image_brightness=1.5)
    assert dialog.isVisible()
    dialog.prepare_start()
    assert parent.service.config.value.source.exposure_ms == 8
    assert parent.service.config.value.tracking.image_brightness == 1.5
    parent.service.run = {'running': True}
    dialog.update_running_state()
    assert not controls.fps.isEnabled()
    assert not dialog.pages['Network'].isEnabled()
    assert controls.exposure.isEnabled()
    parent.service.run = None
    dialog.update_running_state()
    assert controls.fps.isEnabled()
    assert dialog.pages['Network'].isEnabled()
    # Closing immediately after an edit must not lose the pending adjustment.
    controls.brightness.setValue(2)
    dialog.close()
    assert parent.call.call_args.kwargs['image_brightness'] == 2
    assert not dialog.auto_apply.isActive()
    assert not dialog.reply_timer.isActive()
    parent.close()
    app.processEvents()
