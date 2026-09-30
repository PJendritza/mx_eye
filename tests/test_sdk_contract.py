"""The surface the remote client relies on; guards API-compatible refactors."""

import dataclasses
import importlib
import inspect

import pytest
from py_mx_eye import MxEye, MxEyeConfig, Sample

EYE_METHODS = (
    "connect",
    "read",
    "start",
    "stop",
    "status",
    "close",
)

CONFIG_FIELDS = (
    "host",
    "data_port",
    "control_port",
    "timeout",
)


def test_eye_takes_one_config_object():
    """The constructor stays short; its parameters live in a dataclass."""
    assert list(inspect.signature(MxEye.__init__).parameters)[1:] == ["config"]
    assert [field.name for field in dataclasses.fields(MxEyeConfig)] == list(
        CONFIG_FIELDS
    )
    assert dataclasses.is_dataclass(MxEyeConfig)
    assert MxEyeConfig.__dataclass_params__.frozen


def test_defaults_are_reachable_without_a_config():
    eye = MxEye()
    assert eye.config == MxEyeConfig()
    assert eye.config.host == "127.0.0.1"
    assert eye.config.data_port == 5556
    assert eye.config.control_port == 5557
    assert eye.config.timeout == 3.0


def test_the_handle_keeps_the_config_it_was_given():
    """No copying and no rewriting: the config stays the single source."""
    config = MxEyeConfig(host="10.0.0.2")
    assert MxEye(config).config is config


def test_eye_method_surface():
    parameters = inspect.signature(MxEye.read).parameters
    assert parameters["timeout"].default is None
    assert parameters["max_age_ms"].default == 50.0
    assert parameters["require_valid"].default is True
    assert all(callable(getattr(MxEye, name)) for name in EYE_METHODS)
    # The SDK owns no thread, and the with block scopes the stream only.
    assert callable(MxEye.__enter__)
    assert callable(MxEye.__exit__)
    assert not hasattr(MxEye, "session")
    assert not hasattr(MxEye, "latest")
    assert not hasattr(MxEye, "drain")
    assert not hasattr(MxEye, "stats")


def test_read_requires_a_connection():
    """Reading is not silently empty on a handle that never connected."""
    eye = MxEye()
    with pytest.raises(RuntimeError, match="Connect before reading"):
        eye.read(timeout=0.0)


def test_sample_surface():
    assert set(Sample.__dataclass_fields__) == {"frame", "receive_ns"}
    assert Sample.__dataclass_params__.frozen
    for name in ("network_ms", "arrival_age_ms", "age_ms"):
        assert isinstance(getattr(Sample, name), property)
    for name in ("age_ms_at", "is_fresh_at"):
        assert callable(getattr(Sample, name))


def test_legacy_names_are_gone():
    """The SDK entry point is MxEye; no alias keeps the old names alive."""
    with pytest.raises(ImportError):
        importlib.import_module("py_mx_eye.client")
    with pytest.raises(ImportError):
        from py_mx_eye import Client  # noqa: F401
    with pytest.raises(ImportError):
        from py_mx_eye import PyMXEye  # noqa: F401
    with pytest.raises(ImportError):
        from py_mx_eye import Stats  # noqa: F401


def test_legacy_private_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("py_mx_eye.py_mx_eye")
