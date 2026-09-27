"""Typed control models and real TCP request-response integration tests."""

import concurrent.futures
import math
import socket
import threading
import time
from collections import deque
from contextlib import contextmanager

import pytest
from mx_eye import config as cfg
from mx_eye.control_server import ControlServer
from mx_eye.service import Service
from mx_eye_protocol.control import (
    Command,
    NetworkStatus,
    Reply,
    Request,
    SourceMode,
    SourceStatus,
    StatusSnapshot,
    TrackingStats,
    Transport,
)
from mx_eye_protocol.json_io import receive_json
from py_mx_eye import Client
from py_mx_eye.control import ClockSync, ControlClient
from pydantic import ValidationError


@contextmanager
def running_server(dispatch):
    with ControlServer(("127.0.0.1", 0), dispatch) as server:
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=2)
            assert not thread.is_alive()


def test_typed_json_round_trip():
    reply = Reply(
        status=StatusSnapshot(
            state="running",
            stats=TrackingStats(acquired=12, tracked=10),
            network=NetworkStatus(control_port=6000),
            source=SourceStatus(width=640, mode=SourceMode.CAMERA),
        )
    )
    decoded = Reply.model_validate_json(reply.model_dump_json())
    assert decoded.status.state == "running"
    assert decoded.status.stats.tracked == 10
    assert decoded.status.network.control_port == 6000
    assert decoded.status.network.transport is Transport.TCP
    assert decoded.status.source.width == 640
    assert decoded.status.source.mode is SourceMode.CAMERA
    request = Request.model_validate_json('{"command":"sync","t1":123}')
    assert request.command is Command.SYNC
    assert request.t1 == 123
    assert request.protocol == "1.0.0"
    assert '"protocol":"1.0.0"' in request.model_dump_json()


def test_config_and_sdk_share_protocol_enums():
    assert cfg.Transport is Transport
    assert cfg.SourceMode is SourceMode
    assert cfg.NetworkConfig().transport is Transport.TCP
    assert Client(transport="udp").transport is Transport.UDP
    assert Client(transport=Transport.TCP).transport is Transport.TCP
    assert (
        '"transport":"udp"' in NetworkStatus(transport=Transport.UDP).model_dump_json()
    )
    with pytest.raises(ValidationError):
        NetworkStatus(transport="http")
    with pytest.raises(ValidationError):
        SourceStatus(mode="file")


@pytest.mark.parametrize(
    "version",
    [
        1,
        "1",
        "1.0",
        "01.0.0",
        "1.0.1",
        "2.0.0",
        "1.0.0-rc.1",
    ],
)
def test_unsupported_protocol_versions(version):
    with pytest.raises(ValidationError):
        Request(command=Command.STATUS, protocol=version)


@pytest.mark.parametrize(
    "data",
    [
        "{}",
        '{"command":"unknown"}',
        '{"command":"status","protocol":2}',
        '{"command":"sync"}',
        '{"command":"sync","t1":-1}',
        '{"command":"sync","t1":"123"}',
        '{"command":"sync","t1":true}',
        '{"command":"start","t1":1}',
        '{"command":"stop","extra":1}',
    ],
)
def test_invalid_requests(data):
    with pytest.raises(ValidationError):
        Request.model_validate_json(data)


@pytest.mark.parametrize(
    "data",
    [
        "{}",
        '{"ok":false}',
        '{"ok":true,"error":"bad"}',
        '{"ok":false,"error":"bad","status":{"state":"idle"}}',
        '{"ok":true,"status":{"state":"idle"},"sync":{"t1":1,"t2":2,"t3":3}}',
        '{"ok":true,"sync":{"t1":1,"t2":2}}',
    ],
)
def test_invalid_responses(data):
    with pytest.raises(ValidationError):
        Reply.model_validate_json(data)


def test_all_commands_share_endpoint(monkeypatch):
    commands = []
    service = Service.__new__(Service)
    service.snapshot = lambda: StatusSnapshot(state="idle")

    def submit(command):
        commands.append(command)
        result = concurrent.futures.Future()
        result.set_result(
            StatusSnapshot(state="running" if command is Command.START else "stopping")
        )
        return result

    service.submit = submit
    with running_server(service._dispatch_request) as server:
        port = server.server_address[1]
        client = Client(control_port=port)
        # This test isolates command transport from the separate sample stream.
        monkeypatch.setattr(client, "connect", lambda: client)
        assert client.start().state == "running"
        assert client.stop().state == "stopping"
        assert client.status().network.control_port == 5557
        reply = ControlClient("127.0.0.1", port, 3.0).rpc(
            Request(command=Command.SYNC, t1=123)
        )
        assert reply.sync.t1 == 123
        assert reply.sync.t3 >= reply.sync.t2
        assert commands == [Command.START, Command.STOP]


@pytest.mark.parametrize(
    "data",
    [
        b"not json\n",
        b'{"command":"unknown"}\n',
        b'{"command":"sync"}\n',
        b'{"command":"status","protocol":2}\n',
        b'{"command":"status","protocol":1}\n',
        b'{"command":"status","protocol":"1.0.1"}\n',
        b"x" * 16385 + b"\n",
    ],
)
def test_server_replies_to_malformed_requests(data):
    calls = []

    def dispatch(request, received_ns):
        calls.append(request)
        return Reply(status=StatusSnapshot(state="idle"))

    with running_server(dispatch) as server:
        with socket.create_connection(server.server_address, timeout=2) as sock:
            sock.sendall(data)
            reply = receive_json(sock, Reply)
            assert not reply.ok
            assert reply.error
        client = Client(control_port=server.server_address[1])
        assert client.status().state == "idle"
    assert len(calls) == 1


def test_fragmented_request_and_connection_closure():
    def dispatch(request, received_ns):
        assert request.command is Command.STATUS
        return Reply(status=StatusSnapshot(state="idle"))

    with (
        running_server(dispatch) as server,
        socket.create_connection(server.server_address, timeout=2) as sock,
    ):
        sock.sendall(b'{"comm')
        sock.sendall(b'and":"status"}\n')
        assert receive_json(sock, Reply).status.state == "idle"
        assert sock.recv(1) == b""


def test_handler_error_is_a_reply():
    def dispatch(request, received_ns):
        raise RuntimeError("Camera unavailable")

    with running_server(dispatch) as server:
        client = Client(control_port=server.server_address[1])
        with pytest.raises(RuntimeError, match="Camera unavailable"):
            client.stop()


def test_slow_start_does_not_block_sync():
    entered = threading.Event()
    release = threading.Event()
    service = Service.__new__(Service)

    def submit(command):
        entered.set()
        result = concurrent.futures.Future()
        assert release.wait(2)
        result.set_result(StatusSnapshot(state="running"))
        return result

    service.submit = submit
    with running_server(service._dispatch_request) as server:
        control = ControlClient("127.0.0.1", server.server_address[1], 1.0)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            start = executor.submit(control.rpc, Request(command=Command.START))
            try:
                assert entered.wait(1)
                sync = control.rpc(Request(command=Command.SYNC, t1=456))
                assert sync.sync.t1 == 456
                assert not start.done()
            finally:
                release.set()
            assert start.result(timeout=2).status.state == "running"


def test_background_clock_sync_uses_control_port():
    service = Service.__new__(Service)
    with running_server(service._dispatch_request) as server:
        clock = ClockSync(ControlClient("127.0.0.1", server.server_address[1], 3.0))
        clock.start()
        try:
            deadline = time.monotonic() + 2
            while not clock.synced and time.monotonic() < deadline:
                time.sleep(0.005)
            assert clock.synced
            assert clock.rtt_ms >= 0
        finally:
            clock.stop()


class FakeControl:
    """Return scripted four-timestamp probes, one per call."""

    def __init__(self, probes):
        self.probes = deque(probes)

    def probe_timestamps(self, timeout):
        return self.probes.popleft() if self.probes else None


def test_clock_sync_keeps_the_lowest_rtt_probe():
    clock = ClockSync(
        FakeControl([(0, 1000, 1000, 1200), (0, 100, 100, 300), (0, 900, 900, 1800)]),
        probes=3,
        now=lambda: 1_000_000_000,
    )
    assert clock.run_once()
    state = clock.state()
    assert state.rtt_ms == 300 / 1e6
    assert state.offset_ns == -50.0
    assert state.valid_until_ns == 1_000_000_000 + 45_000_000_000
    assert clock.synced


def test_clock_sync_discards_unusable_probes():
    assert not ClockSync(FakeControl([]), now=lambda: 0).run_once()
    # A negative round trip and one above the 300 ms cap are both discarded.
    unusable = [(100, 0, 0, 0), (0, 0, 0, 400_000_000)]
    clock = ClockSync(FakeControl(unusable), probes=2, now=lambda: 0)
    assert not clock.run_once()
    assert not clock.synced
    assert math.isnan(clock.rtt_ms)


def test_clock_sync_expires_without_a_fresh_probe():
    now = [0]
    clock = ClockSync(FakeControl([(0, 0, 0, 0)]), probes=1, now=lambda: now[0])
    assert clock.run_once()
    assert clock.synced
    now[0] = 45_000_000_000 + 1
    assert not clock.synced
    # The last round-trip measurement is reported even after the estimate expires.
    assert clock.rtt_ms == 0.0


def test_probe_swallows_transport_failures():
    def refuse(address, timeout):
        raise OSError("connection refused")

    control = ControlClient("127.0.0.1", 1, 1.0, connect=refuse, now=lambda: 0)
    assert control.probe_timestamps(0.3) is None


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_service_status_reconfigure_and_shutdown():
    config = cfg.MxEyeConfigStore.defaults()
    config.value.network.control_port = _free_port()
    service = Service(config)
    try:
        assert not service._server_errors
        client = Client(control_port=config.value.network.control_port)
        status = client.status()
        assert status.state == "idle"
        assert status.stats.acquired == 0
        assert status.network.control_port == config.value.network.control_port
        assert status.source.width == 0
        control = ControlClient("127.0.0.1", config.value.network.control_port, 3.0)
        assert control.rpc(Request(command=Command.SYNC, t1=7)).sync.t1 == 7

        updated = cfg.MxEyeConfigStore(config.value.model_copy(deep=True))
        updated.value.network.control_port = _free_port()
        result = service.submit("settings", config=updated).result(timeout=3)
        assert result.network.control_port == updated.value.network.control_port
        assert Client(control_port=result.network.control_port).status().state == "idle"
    finally:
        service.close()
    assert service._server is None
    assert not service._owner.is_alive()


def test_legacy_sync_port_is_no_longer_a_setting():
    with pytest.raises(ValidationError):
        cfg.NetworkConfig(sync_port=5558)
