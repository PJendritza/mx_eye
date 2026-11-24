"""Head compensation (x) as defined by mx_calibrator's calibration profiles."""

import json
import math
import time

import numpy as np
import pytest
from mx_eye import config as cfg
from mx_eye.calibration import CalibrationProfile, CalibrationStore
from mx_eye.service import Service
from mx_eye_protocol import TrackingFlags
from py_mx_eye import MxEye, MxEyeConfig
from test_sdk_read import _free_port


def profile(**changes):
    values = dict(
        animal_id="marmoset-1", screen_width=1024, screen_height=600,
        gain_x=-54.88, gain_y=30.49, rotation_deg=90, offset_x=174, offset_y=146,
    )
    values.update(changes)
    return CalibrationProfile(**values)


def test_compensation_subtracts_head_trend_before_the_transform():
    compensated = profile(head_comp_x_slope=0.35, head_comp_x_ref=640.0)
    plain = profile()
    for raw_x, raw_y, cr_x in np.random.default_rng(3).uniform((-20, -20, 600), (20, 20, 700), (50, 3)):
        corrected = raw_x - 0.35 * (cr_x - 640.0)
        assert compensated.apply(raw_x, raw_y, cr_x) == pytest.approx(plain.apply(corrected, raw_y))
    # At the reference head position nothing changes.
    assert compensated.apply(3.0, -2.0, 640.0) == pytest.approx(plain.apply(3.0, -2.0))


def test_zero_slope_is_off_and_needs_no_corneal_reflection():
    plain = profile()
    assert not plain.head_compensated
    assert plain.apply(3.0, -2.0) == plain.apply(3.0, -2.0, 655.0)


def test_compensation_without_corneal_reflection_has_no_position():
    compensated = profile(head_comp_x_slope=0.35, head_comp_x_ref=640.0)
    assert all(math.isnan(v) for v in compensated.apply(3.0, -2.0))
    assert all(math.isnan(v) for v in compensated.apply(3.0, -2.0, math.nan))


def test_mx_calibrator_export_imports_with_its_head_compensation(tmp_path):
    # Key layout written by mx_calibrator's export_profile for a compensated profile.
    exported = profile(head_comp_x_slope=0.35, head_comp_x_ref=640.0).model_dump(mode="json")
    path = tmp_path / "exported.json"
    path.write_text(json.dumps(exported))
    store = CalibrationStore(tmp_path / "local")
    imported = store.import_profile(path)
    assert (imported.head_comp_x_slope, imported.head_comp_x_ref) == (0.35, 640.0)
    assert str(imported.calibration_id) == exported["calibration_id"]
    assert CalibrationStore(store.root).load_last_active() == imported


def test_profiles_without_compensation_keep_the_original_file_keys(tmp_path):
    store = CalibrationStore(tmp_path)
    plain = json.loads(store.save_and_activate(profile()).read_text("utf-8"))
    assert "head_comp_x_slope" not in plain and "head_comp_x_ref" not in plain
    saved = json.loads(store.save_and_activate(profile(head_comp_x_slope=0.2)).read_text("utf-8"))
    assert saved["head_comp_x_slope"] == 0.2 and saved["head_comp_x_ref"] == 0.0


def test_simulation_sends_head_compensated_screen_position(tmp_path):
    k, reference = 0.5, 300.0
    config = cfg.MxEyeConfigStore.defaults()
    config.value.source.mode = cfg.SourceMode.SIMULATION
    config.value.source.fps = 30
    config.value.calibration = CalibrationProfile(
        animal_id="simulation", head_comp_x_slope=k, head_comp_x_ref=reference)
    config.value.recording.directory = str(tmp_path)
    config.value.network.data_port = _free_port()
    config.value.network.control_port = _free_port()
    service = Service(config)
    eye = MxEye(MxEyeConfig(data_port=config.value.network.data_port,
                            control_port=config.value.network.control_port))
    try:
        eye.connect()
        eye.start()
        deadline = time.monotonic() + 5
        while True:
            frame = next(eye.read(timeout=3, max_age_ms=None, require_valid=False)).frame
            if frame.flags & TrackingFlags.CALIBRATED:
                break
            if time.monotonic() >= deadline:
                pytest.fail("Simulation did not produce calibrated output")
        raw_x = frame.pupil_x - frame.cr_x
        raw_y = frame.pupil_y - frame.cr_y
        expected_x = 512 + raw_x - k * (frame.cr_x - reference)
        assert frame.x == pytest.approx(expected_x, abs=1e-3)
        assert frame.y == pytest.approx(300 + raw_y, abs=1e-3)
        assert frame.flags & TrackingFlags.VALID
    finally:
        eye.close()
        service.close()


def test_calibration_window_loads_and_edits_head_compensation(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets as W
    from mx_eye.calibration_gui import CalibrationWindow

    app = W.QApplication.instance() or W.QApplication([])
    store = CalibrationStore(tmp_path / "local")
    window = CalibrationWindow(store)
    compensated = profile(head_comp_x_slope=0.35, head_comp_x_ref=640.0)
    path = tmp_path / "calibrator.json"
    path.write_text(compensated.model_dump_json())
    monkeypatch.setattr(W.QFileDialog, "getOpenFileName", lambda *a: (str(path), "JSON"))
    window._import_profile()
    assert (window.head_slope.value(), window.head_ref.value()) == (0.35, 640.0)
    assert window._profile_from_controls() == compensated
    # A typed k reaches the live profile, and preview samples use each CR x.
    emitted = []
    window.profileChanged.connect(emitted.append)
    window.head_slope.setValue(0.5)
    assert emitted[-1].head_comp_x_slope == 0.5
    window.update_sample(3.0, -2.0, 700.0)
    expected = window._display_point(*emitted[-1].apply(3.0, -2.0, 700.0))
    assert tuple(window.current.getData()[0]) + tuple(window.current.getData()[1]) == pytest.approx(expected)
    window.update_sample(3.0, -2.0)  # no CR: not plotted, nothing raised
    window._set_current_as_center()
    assert "corneal reflection" in window.status.text()
    window.close()
    app.processEvents()
