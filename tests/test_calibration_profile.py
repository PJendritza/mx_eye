import json
import math
import re

import pytest
from mx_eye.calibration import CalibrationProfile, CalibrationStore


def test_manual_transform_maps_origin_to_screen_center():
    profile = CalibrationProfile(
        animal_id="marmoset-1",
        screen_width=1024,
        screen_height=600,
        gain_x=2,
        gain_y=3,
    )

    assert profile.apply(0, 0) == (512, 300)
    assert profile.apply(1, 1) == pytest.approx((514, 303))


def test_manual_transform_rotates_and_offsets():
    profile = CalibrationProfile(
        animal_id="marmoset-1",
        screen_width=100,
        screen_height=80,
        rotation_deg=90,
        offset_x=5,
        offset_y=-2,
    )

    assert profile.apply(10, 0) == pytest.approx((48, 55))
    assert all(math.isnan(value) for value in profile.apply(math.nan, 0))


@pytest.mark.parametrize("rotation", [0, 88, 90])
@pytest.mark.parametrize("offset_x, offset_y", [(30, 0), (0, 20), (-30, -20)])
def test_physical_offsets_shift_gaze_after_rotation(rotation, offset_x, offset_y):
    plain = CalibrationProfile(
        animal_id="marmoset-1", gain_x=-54.88, gain_y=30.49,
        rotation_deg=rotation, head_comp_x_slope=0.35, head_comp_x_ref=640,
    )
    shifted = plain.model_copy(update={"offset_x": offset_x, "offset_y": offset_y})
    sample = (3.0, -2.0, 700.0)
    base_x, base_y = plain.apply(*sample)
    assert shifted.apply(*sample) == pytest.approx((base_x + offset_y, base_y + offset_x))
    assert shifted.physical_point(*shifted.apply(*sample)) == pytest.approx(
        (base_y - 300 + offset_x, base_x - 512 + offset_y)
    )


def test_store_versions_profiles_and_loads_active(tmp_path):
    store = CalibrationStore(tmp_path)
    original = CalibrationProfile(animal_id="animal/a", gain_x=2)

    first = store.save_and_activate(original)
    second = store.save_and_activate(original.model_copy(update={"gain_x": 3}))

    assert first != second
    assert first.exists() and second.exists()
    saved = json.loads(first.read_text("utf-8"))
    assert saved["version"] == "1.2.0"
    assert saved["offset_coordinate_system"] == "physical_screen_y_up"
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-\d{6}\.json",
        first.name,
    )
    active = store.load_active("animal/a")
    assert active is not None
    assert active.gain_x == 3
    assert store.load_last_active() == active
    index = json.loads((tmp_path / "active.json").read_text("utf-8"))
    assert index["last_animal"] == "animal/a"
