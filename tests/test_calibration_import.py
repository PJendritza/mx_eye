import json

import numpy as np
import pytest

from mx_eye import config as cfg
from mx_eye.calibration import CalibrationProfile, CalibrationStore
from mx_eye.service import Service


def example():
    # The profile exported by mx_calibrator for Patrick's real recording.
    return CalibrationProfile(
        animal_id='default', screen_width=1024, screen_height=600,
        gain_x=-54.88, gain_y=30.49, rotation_deg=90, offset_x=174, offset_y=146,
    )


def test_import_preserves_identity_and_exact_flip_rotation(tmp_path):
    profile = example()
    path = tmp_path / 'external.json'
    path.write_text(profile.model_dump_json())
    store = CalibrationStore(tmp_path / 'local')
    imported = store.import_profile(path)
    assert imported == profile
    path.unlink()  # Restoring does not depend on the external/network file.
    assert CalibrationStore(store.root).load_last_active() == profile
    for x, y in np.random.default_rng(42).uniform(-20, 20, (100, 2)):
        assert imported.apply(x, y) == pytest.approx((686 - 30.49*y, 446 - 54.88*x))


def test_source_and_configuration_changes_keep_selected_calibration():
    service = Service.__new__(Service)
    service.run = None
    service.config = cfg.MxEyeConfigStore.defaults()
    profile = example()
    service.config.value.calibration = profile
    service.snapshot = lambda: None
    incoming = cfg.MxEyeConfigStore.defaults()
    incoming.value.source.mode = cfg.SourceMode.VIDEO
    incoming.value.source.path = 'another-video.mkv'
    incoming.value.calibration = profile.model_copy(update={'gain_x': 99})
    service._execute('settings', {'config': incoming})
    assert service.config.value.source.path == 'another-video.mkv'
    assert service.config.value.calibration == profile
    incoming.value.calibration = None
    incoming.value.source.mode = cfg.SourceMode.CAMERA
    service._execute('settings', {'config': incoming})
    assert service.config.value.calibration == profile
    tracking = service.config.value.tracking.model_copy(update={'pupil_thr': 55})
    service._execute('config', {'tracking': tracking})
    assert service.config.value.calibration == profile


def test_wrong_tracking_mode_keeps_profile_but_is_not_compatible():
    profile = example()
    tracking = cfg.TrackingConfig()
    assert profile.matches_tracking(tracking)
    tracking.tracking_mode = cfg.TrackingMode.PUPIL_ONLY
    assert not profile.matches_tracking(tracking)
    assert profile.model_copy(update={'input_coordinate_system': 'image_absolute'}).matches_tracking(tracking)
    tracking.pupil_coordinates = cfg.PupilCoordinates.RELATIVE
    assert profile.model_copy(update={'input_coordinate_system': 'roi_relative'}).matches_tracking(tracking)


def test_load_button_updates_view_and_remembers_profile(tmp_path, monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    from PySide6 import QtWidgets as W
    from mx_eye.calibration_gui import CalibrationWindow
    app = W.QApplication.instance() or W.QApplication([])
    store = CalibrationStore(tmp_path / 'local')
    window = CalibrationWindow(store)
    profile = example()
    path = tmp_path / 'calibrator.json'
    path.write_text(profile.model_dump_json())
    monkeypatch.setattr(W.QFileDialog, 'getOpenFileName', lambda *a: (str(path), 'JSON'))
    emitted = []
    window.profileChanged.connect(emitted.append)
    window._import_profile()
    assert emitted[-1] == profile
    assert window._profile_from_controls() == profile
    assert window._display_point(*profile.apply(1, 0)) == pytest.approx((391.12, 338))
    assert store.load_last_active() == profile
    # An invalid replacement must leave the active profile untouched.
    path.write_text(json.dumps({'format': 'wrong'}))
    warnings = []
    monkeypatch.setattr(W.QMessageBox, 'warning', lambda *a: warnings.append(a))
    window._import_profile()
    assert warnings and window.profile == profile
    assert store.load_last_active() == profile
    window.close()
    app.processEvents()
