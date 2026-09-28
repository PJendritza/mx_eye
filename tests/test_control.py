"""Typed control models and real TCP request-response integration tests."""

import concurrent.futures
import socket
import threading
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
from py_mx_eye import MxEye, MxEyeConfig
from py_mx_eye.control import ControlClient
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
    request = Request.model_validate_json('{"command":"status"}')
    assert request.command is Command.STATUS
    assert request.protocol == "1.0.0"
    assert '"protocol":"1.0.0"' in request.model_dump_json()


def test_config_and_sdk_share_protocol_enums():
    assert cfg.Transport is Transport
    assert cfg.SourceMode is SourceMode
    assert cfg.NetworkConfig().transport is Transport.TCP
    assert MxEyeConfig().transport is Transport.TCP
    assert MxEyeConfig(transport=Transport.UDP).transport is Transport.UDP
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
        '{"command":"sync","t1":123}',
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


def test_all_commands_share_endpoint():
    commands = []
    service = _session_service(commands)
    with running_server(service._dispatch_request) as server:
        port = server.server_address[1]
        # The control plane is independent of the sample stream, which is never
        # opened here.
        eye = MxEye(MxEyeConfig(control_port=port))
        assert eye.start().state == "running"
        assert eye.stop().state == "stopping"
        assert eye.status().network.control_port == 5557
        assert commands == [Command.START, Command.STOP]


def _session_service(commands):
    """A Service stub that records the commands it is given."""
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
    return service


def test_connect_requires_a_publishing_tracker():
    """The data port exists only while a session runs, so connect can fail."""
    eye = MxEye(MxEyeConfig(data_port=_free_port(), control_port=_free_port()))
    try:
        with pytest.raises(OSError):
            eye.connect()
    finally:
        eye.close()
    unreachable = MxEye(MxEyeConfig(data_port=_free_port(), control_port=_free_port()))
    try:
        with pytest.raises(TimeoutError, match="did not reply"):
            unreachable.start()
    finally:
        unreachable.close()


@pytest.mark.parametrize(
    "data",
    [
        b"not json\n",
        b'{"command":"unknown"}\n',
        b'{"command":"sync","t1":123}\n',
        b'{"command":"status","protocol":2}\n',
        b'{"command":"status","protocol":1}\n',
        b'{"command":"status","protocol":"1.0.1"}\n',
        b"x" * 16385 + b"\n",
    ],
)
def test_server_replies_to_malformed_requests(data):
    calls = []

    def dispatch(request):
        calls.append(request)
        return Reply(status=StatusSnapshot(state="idle"))

    with running_server(dispatch) as server:
        with socket.create_connection(server.server_address, timeout=2) as sock:
            sock.sendall(data)
            reply = receive_json(sock, Reply)
            assert not reply.ok
            assert reply.error
        client = MxEye(MxEyeConfig(control_port=server.server_address[1]))
        assert client.status().state == "idle"
    assert len(calls) == 1


def test_fragmented_request_and_connection_closure():
    def dispatch(request):
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
    def dispatch(request):
        raise RuntimeError("Camera unavailable")

    with running_server(dispatch) as server:
        client = MxEye(MxEyeConfig(control_port=server.server_address[1]))
        with pytest.raises(RuntimeError, match="Camera unavailable"):
            client.stop()


def test_slow_start_does_not_block_status_requests():
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
    service.snapshot = lambda: StatusSnapshot(state="idle")
    with running_server(service._dispatch_request) as server:
        control = ControlClient("127.0.0.1", server.server_address[1], 1.0)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            start = executor.submit(control.rpc, Request(command=Command.START))
            try:
                assert entered.wait(1)
                status = control.request_status(Command.STATUS)
                assert status.state == "idle"
                assert not start.done()
            finally:
                release.set()
            assert start.result(timeout=2).status.state == "running"


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
        client = MxEye(MxEyeConfig(control_port=config.value.network.control_port))
        status = client.status()
        assert status.state == "idle"
        assert status.stats.acquired == 0
        assert status.network.control_port == config.value.network.control_port
        assert status.source.width == 0
        control = ControlClient("127.0.0.1", config.value.network.control_port, 3.0)
        assert control.request_status(Command.STATUS).state == "idle"

        updated = cfg.MxEyeConfigStore(config.value.model_copy(deep=True))
        updated.value.network.control_port = _free_port()
        result = service.submit("settings", config=updated).result(timeout=3)
        assert result.network.control_port == updated.value.network.control_port
        assert (
            MxEye(MxEyeConfig(control_port=result.network.control_port)).status().state
            == "idle"
        )
    finally:
        service.close()
    assert service._server is None
    assert not service._owner.is_alive()


def test_legacy_sync_port_is_no_longer_a_setting():
    with pytest.raises(ValidationError):
        cfg.NetworkConfig(sync_port=5558)
