"""The surface the remote client relies on; guards API-compatible refactors."""

import importlib
import inspect

import pytest
from mx_eye_protocol.control import Transport
from py_mx_eye import Client, Sample, Stats

# Fields read by mx_eye_client.receiver.refresh().
CONSUMER_STATS_FIELDS = {
    "received",
    "clock_synced",
    "sync_rtt_ms",
    "sequence_gaps",
    "acquisition_skips",
    "buffer_overwrites",
    "error",
}

CLIENT_METHODS = ("connect", "start", "stop", "status", "latest", "drain", "close")


def test_client_constructor_surface():
    parameters = inspect.signature(Client.__init__).parameters
    assert list(parameters)[1:8] == [
        "host",
        "data_port",
        "control_port",
        "transport",
        "udp_bind",
        "buffer_samples",
        "timeout",
    ]
    assert parameters["timeout"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert parameters["now"].kind is inspect.Parameter.KEYWORD_ONLY
    # The remote client passes the host and both ports positionally.
    client = Client("10.0.0.2", 6000, 6001)
    assert (client.host, client.data_port, client.control_port) == (
        "10.0.0.2",
        6000,
        6001,
    )
    assert client.transport is None
    assert Client(transport="udp").transport is Transport.UDP
    assert Client(transport=Transport.TCP).transport is Transport.TCP


def test_client_method_surface():
    parameters = inspect.signature(Client.latest).parameters
    assert parameters["max_age_ms"].default == 50.0
    assert parameters["require_valid"].default is True
    assert all(callable(getattr(Client, name)) for name in CLIENT_METHODS)
    client = Client()
    assert client.latest() is None
    assert client.drain() == []
    assert client.stats.received == 0


def test_stats_model_covers_the_consumer():
    stats = Client().stats
    assert isinstance(stats, Stats)
    assert CONSUMER_STATS_FIELDS <= set(type(stats).model_fields)
    assert set(type(stats).model_fields) == CONSUMER_STATS_FIELDS | {
        "malformed",
        "out_of_order",
    }
    assert stats.error == ""
    assert stats.clock_synced is False
    # NaN is a legitimate value before the first synchronization.
    assert stats.sync_rtt_ms != stats.sync_rtt_ms


def test_sample_surface():
    assert set(Sample.__dataclass_fields__) == {
        "frame",
        "receive_ns",
        "clock_offset_ns",
        "clock_valid_until_ns",
        "sync_rtt_ms",
    }
    assert Sample.__dataclass_params__.frozen
    for name in ("clock_valid", "network_ms", "arrival_age_ms", "age_ms"):
        assert isinstance(getattr(Sample, name), property)
    for name in ("clock_valid_at", "age_ms_at", "is_fresh_at"):
        assert callable(getattr(Sample, name))


def test_legacy_private_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("py_mx_eye.py_mx_eye")
