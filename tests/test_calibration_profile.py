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
        raw_origin_x=12,
        raw_origin_y=-4,
        gain_x=2,
        gain_y=3,
    )

    assert profile.apply(12, -4) == (512, 300)
    assert profile.apply(13, -3) == pytest.approx((514, 303))


def test_manual_transform_rotates_and_offsets():
    profile = CalibrationProfile(
        animal_id="marmoset-1",
        screen_width=100,
        screen_height=80,
        rotation_deg=90,
        offset_x=5,
        offset_y=-2,
    )

    assert profile.apply(10, 0) == pytest.approx((55, 48))
    assert all(math.isnan(value) for value in profile.apply(math.nan, 0))


def test_store_versions_profiles_and_loads_active(tmp_path):
    store = CalibrationStore(tmp_path)
    original = CalibrationProfile(animal_id="animal/a", gain_x=2)

    first = store.save_and_activate(original)
    second = store.save_and_activate(original.model_copy(update={"gain_x": 3}))

    assert first != second
    assert first.exists() and second.exists()
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
