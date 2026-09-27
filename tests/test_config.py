import json

import pytest
from mx_eye import config as cfg
from pydantic import ValidationError


def test_defaults_are_complete_and_independent():
    first = cfg.MxEyeConfigStore.defaults()
    second = cfg.MxEyeConfigStore.defaults()

    assert first.value.format == "mx-eye"
    assert first.value.version == "1.0.0"
    assert first.value.source.fps == 120.0
    assert first.value.tracking.pupil_coordinates == "absolute"
    assert first.value.template is None
    assert first.value.display.pupil_mask is None

    first.value.tracking.roi = (99, 0, 320, 240)
    assert second.value.tracking.roi == (0, 0, 320, 240)


@pytest.mark.parametrize(
    ("group", "key", "value"),
    [
        ("source", "mode", "stream"),
        ("source", "width", 31),
        ("source", "fps", float("inf")),
        ("source", "fourcc", "RGB"),
        ("tracking", "pupil_thr", 256),
        ("tracking", "pupil_gate", 0),
        ("tracking", "template_min_corr", 1.1),
        ("tracking", "tracking_mode", "CR only"),
        ("network", "data_port", 80),
        ("network", "transport", "serial"),
        ("recording", "buffer_mb", 7),
        ("recording", "codec", "H264"),
        ("display", "hz", 0),
    ],
)
def test_assignment_rejects_invalid_fields(group, key, value):
    config = cfg.MxEyeConfigStore.defaults()

    with pytest.raises(ValidationError):
        setattr(getattr(config.value, group), key, value)


def test_validation_rejects_cross_field_errors():
    duplicate_ports = cfg.MxEyeConfigStore.defaults().value.model_dump()
    duplicate_ports["network"]["control_port"] = duplicate_ports["network"]["data_port"]
    with pytest.raises(ValidationError, match="two distinct ports"):
        cfg.MxEyeConfigModel.model_validate(duplicate_ports)

    reversed_areas = cfg.MxEyeConfigStore.defaults().value.model_dump()
    reversed_areas["tracking"]["pupil_min"] = 50
    reversed_areas["tracking"]["pupil_max"] = 49
    with pytest.raises(ValidationError, match="Minimum blob area"):
        cfg.MxEyeConfigModel.model_validate(reversed_areas)

    invalid_roi = cfg.MxEyeConfigStore.defaults().value.model_dump()
    invalid_roi["tracking"]["roi"] = [0, 0, 0, 240]
    with pytest.raises(ValidationError, match="ROI width and height"):
        cfg.MxEyeConfigModel.model_validate(invalid_roi)


@pytest.mark.parametrize(
    "path",
    [
        ("unexpected",),
        ("source", "unexpected"),
        ("tracking", "unexpected"),
        ("network", "unexpected"),
        ("recording", "unexpected"),
        ("display", "unexpected"),
        ("template", "unexpected"),
    ],
)
def test_validation_rejects_unknown_fields_with_a_path(path):
    raw = cfg.MxEyeConfigStore.defaults().value.model_dump()
    if path[0] == "template":
        raw["template"] = {
            "png": "data",
            "anchor": [1, 2],
            "center": [3, 4],
            "unexpected": True,
        }
    elif len(path) == 1:
        raw[path[0]] = True
    else:
        raw[path[0]][path[1]] = True

    with pytest.raises(ValidationError) as error:
        cfg.MxEyeConfigModel.model_validate(raw)

    assert ".".join(path) in str(error.value)


def test_load_overlays_partial_current_config(tmp_path):
    path = tmp_path / "partial.json"
    path.write_text(
        json.dumps({"source": {"mode": "simulation"}, "display": {"masks": False}}),
        encoding="utf-8",
    )

    loaded = cfg.MxEyeConfigStore.load(path)

    assert loaded.value.source.mode == "simulation"
    assert loaded.value.source.width == 640
    assert loaded.value.display.masks is False
    assert loaded.value.network == cfg.MxEyeConfigStore.defaults().value.network


def test_paths_must_be_path_objects():
    config = cfg.MxEyeConfigStore.defaults()

    with pytest.raises(TypeError, match="pathlib.Path"):
        cfg.MxEyeConfigStore.load("config.json")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="pathlib.Path"):
        config.save("config.json")  # type: ignore[arg-type]


def test_save_and_load_round_trip(tmp_path):
    config = cfg.MxEyeConfigStore.defaults()
    config.value.display.pupil_mask = False
    config.value.display.cr_mask = True
    config.value.display.template_circle = False
    config.value.display.template_inset = True
    config.value.template = cfg.TemplateConfig(
        png="encoded-png",
        anchor=(12.5, 14),
        center=(18, 20.5),
    )
    original = cfg.MxEyeConfigStore(config.value.model_copy(deep=True))
    path = tmp_path / "nested" / "config.json"

    config.save(path)

    assert cfg.MxEyeConfigStore.load(path).value == original.value
    assert config.value == original.value
    assert not path.with_suffix(".json.tmp").exists()


def test_shared_store_is_a_process_singleton():
    shared = cfg.store()

    assert shared is cfg.store()
    shared.value.source.mode = cfg.SourceMode.SIMULATION
    assert cfg.store().value.source.mode is cfg.SourceMode.SIMULATION

    assert cfg.configure() is shared
    assert cfg.store().value.source.mode is cfg.SourceMode.CAMERA


def test_configure_loads_a_file_into_the_shared_store(tmp_path):
    path = tmp_path / "shared.json"
    path.write_text(json.dumps({"source": {"mode": "simulation"}}), encoding="utf-8")

    shared = cfg.store()

    assert cfg.configure(path) is shared
    assert cfg.store().value.source.mode is cfg.SourceMode.SIMULATION
    assert cfg.store().value.source.width == 640

    cfg.configure()
    assert cfg.store().value.source.mode is cfg.SourceMode.CAMERA
