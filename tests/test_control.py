"""Typed control models and real ZeroMQ REQ/REP integration tests."""

import concurrent.futures
import socket
import threading
from contextlib import contextmanager

import pytest
import zmq
from mx_eye import config as cfg
from mx_eye.control_server import ControlServer
from mx_eye.service import Service
from mx_eye_protocol.control import (
    Command,
    ControlReply,
    ControlRequest,
    NetworkStatus,
    SourceMode,
    SourceStatus,
    StatusSnapshot,
    TrackingStats,
)
from py_mx_eye import MxEye, MxEyeConfig
from py_mx_eye.control import ControlClient
from pydantic import ValidationError


@contextmanager
def running_server(dispatch):
    with ControlServer(("127.0.0.1", 0), dispatch) as server:
        yield server


def test_typed_json_round_trip():
    reply = ControlReply(
        status=StatusSnapshot(
            state="running",
            stats=TrackingStats(acquired=12, tracked=10),
            network=NetworkStatus(control_port=6000),
            source=SourceStatus(width=640, mode=SourceMode.CAMERA),
        )
    )
    decoded = ControlReply.model_validate_json(reply.model_dump_json())
    assert decoded.status.state == "running"
    assert decoded.status.stats.tracked == 10
    assert decoded.status.network.control_port == 6000
    assert decoded.status.source.width == 640
    assert decoded.status.source.mode is SourceMode.CAMERA
    request = ControlRequest.model_validate_json('{"command":"status"}')
    assert request.command is Command.STATUS
    assert request.protocol == "1.0.0"
    assert '"protocol":"1.0.0"' in request.model_dump_json()


def test_config_and_sdk_share_protocol_enums():
    assert cfg.SourceMode is SourceMode
    with pytest.raises(ValidationError):
        NetworkStatus(transport="tcp")
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
        ControlRequest(command=Command.STATUS, protocol=version)


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
        ControlRequest.model_validate_json(data)


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
        ControlReply.model_validate_json(data)


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


def test_subscribe_before_publisher_and_control_timeout():
    """Subscription is asynchronous; control requests still time out."""
    eye = MxEye(MxEyeConfig(data_port=_free_port(), control_port=_free_port()))
    try:
        eye.connect()
        assert list(eye.read(timeout=0.01)) == []
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
    ],
)
def test_server_replies_to_malformed_requests(data):
    calls = []

    def dispatch(request):
        calls.append(request)
        return ControlReply(status=StatusSnapshot(state="idle"))

    with running_server(dispatch) as server:
        with zmq.Context() as context, context.socket(zmq.REQ) as sock:
            sock.setsockopt(zmq.LINGER, 0)
            sock.connect(f"tcp://127.0.0.1:{server.server_address[1]}")
            sock.send(data)
            assert sock.poll(2000)
            reply = ControlReply.model_validate_json(sock.recv())
            assert not reply.ok
            assert reply.error
        client = MxEye(MxEyeConfig(control_port=server.server_address[1]))
        assert client.status().state == "idle"
    assert len(calls) == 1


def test_repeated_requests_on_one_req_socket():
    def dispatch(request):
        assert request.command is Command.STATUS
        return ControlReply(status=StatusSnapshot(state="idle"))

    with (
        running_server(dispatch) as server,
        zmq.Context() as context,
        context.socket(zmq.REQ) as sock,
    ):
        sock.setsockopt(zmq.LINGER, 0)
        sock.connect(f"tcp://127.0.0.1:{server.server_address[1]}")
        for _ in range(2):
            sock.send_string(ControlRequest(command=Command.STATUS).model_dump_json())
            assert sock.poll(2000)
            assert ControlReply.model_validate_json(sock.recv()).status.state == "idle"


def test_handler_error_is_a_reply():
    def dispatch(request):
        raise RuntimeError("Camera unavailable")

    with running_server(dispatch) as server:
        client = MxEye(MxEyeConfig(control_port=server.server_address[1]))
        with pytest.raises(RuntimeError, match="Camera unavailable"):
            client.stop()


def test_slow_start_serializes_status_and_timeout_does_not_poison_next_call():
    entered, release = threading.Event(), threading.Event()

    def dispatch(request):
        if request.command is Command.START:
            entered.set()
            assert release.wait(3)
        return ControlReply(status=StatusSnapshot(state="running"))

    with running_server(dispatch) as server:
        control = ControlClient("127.0.0.1", server.server_address[1], 0.05)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            start = executor.submit(control.request_status, Command.START, 2.0)
            try:
                assert entered.wait(1)
                with pytest.raises(TimeoutError):
                    control.request_status(Command.STATUS)
                assert not start.done()
            finally:
                release.set()
            assert start.result(timeout=2).state == "running"
        assert control.request_status(Command.STATUS, timeout=2).state == "running"


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


def test_client_disconnect_during_execution_does_not_stall_rep():
    entered, release = threading.Event(), threading.Event()

    def dispatch(request):
        if request.command is Command.START:
            entered.set()
            assert release.wait(2)
        return ControlReply(status=StatusSnapshot(state="running"))

    with running_server(dispatch) as server:
        with zmq.Context() as context, context.socket(zmq.REQ) as sock:
            sock.setsockopt(zmq.LINGER, 0)
            sock.connect(f"tcp://127.0.0.1:{server.server_address[1]}")
            sock.send_string(ControlRequest(command=Command.START).model_dump_json())
            assert entered.wait(1)
        release.set()
        client = ControlClient("127.0.0.1", server.server_address[1], 2)
        assert client.request_status(Command.STATUS).state == "running"


def test_multipart_request_is_rejected_and_rep_remains_usable():
    with (
        running_server(
            lambda _: ControlReply(status=StatusSnapshot(state="idle"))
        ) as server,
        zmq.Context() as context,
        context.socket(zmq.REQ) as sock,
    ):
        sock.setsockopt(zmq.LINGER, 0)
        sock.connect(f"tcp://127.0.0.1:{server.server_address[1]}")
        sock.send_multipart([b"{}", b"{}"])
        assert sock.poll(2000)
        assert not ControlReply.model_validate_json(sock.recv()).ok
        sock.send_string(ControlRequest(command=Command.STATUS).model_dump_json())
        assert sock.poll(2000)
        assert ControlReply.model_validate_json(sock.recv()).ok


def test_control_bind_conflict_and_shutdown_release_port():
    with running_server(
        lambda _: ControlReply(status=StatusSnapshot(state="idle"))
    ) as first:
        address = first.server_address
        second = ControlServer(
            address, lambda _: ControlReply(status=StatusSnapshot(state="idle"))
        )
        with pytest.raises(zmq.ZMQError):
            second.start()
        second.close()
    with ControlServer(
        address, lambda _: ControlReply(status=StatusSnapshot(state="idle"))
    ):
        client = ControlClient(*address, timeout=1)
        assert client.request_status(Command.STATUS).state == "idle"


def test_large_control_request_and_reply():
    message = "Status detail " * 2000
    with running_server(
        lambda _: ControlReply(status=StatusSnapshot(state="idle", message=message))
    ) as server:
        with zmq.Context() as context, context.socket(zmq.REQ) as sock:
            sock.setsockopt(zmq.LINGER, 0)
            sock.connect(f"tcp://127.0.0.1:{server.server_address[1]}")
            # Valid JSON whitespace makes the request exceed the former 16 KiB cap.
            sock.send_string(
                " " * 20000 + ControlRequest(command=Command.STATUS).model_dump_json()
            )
            assert sock.poll(2000)
            assert (
                ControlReply.model_validate_json(sock.recv()).status.message == message
            )
        client = ControlClient(*server.server_address, timeout=2)
        assert client.request_status(Command.STATUS).message == message


def test_large_command_error_is_not_truncated():
    message = "Command failure " * 2000

    def dispatch(_):
        raise RuntimeError(message)

    with running_server(dispatch) as server:
        client = ControlClient(*server.server_address, timeout=2)
        with pytest.raises(RuntimeError) as error:
            client.request_status(Command.STATUS)
        assert str(error.value) == message
