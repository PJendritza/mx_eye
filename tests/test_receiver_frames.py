"""Exercise stream framing and datagram rejection through the SDK receiver."""

import socket
import struct
import threading
import time
from collections import deque

import pytest
from mx_eye_protocol import DataFrame, MessageType, TrackingFlags, TrackingPayload
from py_mx_eye import Client

HEADER = struct.Struct("<4sBI")


def _payload(sequence: int) -> TrackingPayload:
    return TrackingPayload(
        session=1,
        sequence=sequence,
        frame=sequence,
        acquisition_ns=1,
        tracking_start_ns=2,
        tracking_end_ns=3,
        send_ns=4,
        media_ns=-1,
        x=1.0,
        y=2.0,
        pupil_x=3.0,
        pupil_y=4.0,
        cr_x=5.0,
        cr_y=6.0,
        pupil_area=7.0,
        template_ncc=0.5,
        flags=TrackingFlags.VALID,
    )


class ScriptedSocket:
    """Supply exact recv boundaries, including partial headers and EOF."""

    def __init__(self, client, chunks):
        self.client = client
        self.chunks = deque(chunks)
        self.closed = False
        self.reads = 0

    def settimeout(self, timeout):
        pass

    def recv(self, size):
        self.reads += 1
        if self.chunks:
            return self.chunks.popleft()
        self.client._stop.set()
        raise TimeoutError

    def close(self):
        self.closed = True


def _run_stream(monkeypatch, chunk_groups):
    client = Client(transport="tcp")
    client._ready = threading.Event()
    sockets = [ScriptedSocket(client, chunks) for chunks in chunk_groups]
    connections = iter(sockets)
    monkeypatch.setattr(
        "py_mx_eye.py_mx_eye.socket.create_connection",
        lambda *args, **kwargs: next(connections),
    )
    client._receive()
    assert client._stats["error"] == ""
    assert client._ready.is_set()
    assert all(sock.closed for sock in sockets)
    return client, sockets


@pytest.mark.parametrize(
    "chunks",
    [
        [
            DataFrame.from_payload(_payload(1)).encode(),
            DataFrame.from_payload(_payload(2)).encode(),
        ],
        [
            DataFrame.from_payload(_payload(1)).encode()
            + DataFrame.from_payload(_payload(2)).encode()
        ],
        [
            DataFrame.from_payload(_payload(1)).encode()[:3],
            DataFrame.from_payload(_payload(1)).encode()[3:8],
            DataFrame.from_payload(_payload(1)).encode()[8:20],
            DataFrame.from_payload(_payload(1)).encode()[20:]
            + DataFrame.from_payload(_payload(2)).encode()[:5],
            DataFrame.from_payload(_payload(2)).encode()[5:],
        ],
        [
            bytes([byte])
            for byte in DataFrame.from_payload(_payload(1)).encode()
            + DataFrame.from_payload(_payload(2)).encode()
        ],
    ],
)
def test_tcp_chunk_boundaries(monkeypatch, chunks):
    client, _ = _run_stream(monkeypatch, [chunks])
    samples = client.drain()
    assert [sample.frame.payload.sequence for sample in samples] == [1, 2]
    assert samples[0].frame.magic == b"MXEY"
    assert samples[0].frame.message_type is MessageType.DATA
    assert samples[0].frame.length == 97
    assert client.latest(max_age_ms=None) is samples[-1]
    assert client.drain() == []
    assert client.stats["malformed"] == 0


@pytest.mark.parametrize(
    "bad_header",
    [
        HEADER.pack(b"FAIL", MessageType.DATA, 97),
        HEADER.pack(b"MXEY", 255, 97),
        HEADER.pack(b"MXEY", MessageType.CMD, 97),
        HEADER.pack(b"MXEY", MessageType.DATA, 0),
        HEADER.pack(b"MXEY", MessageType.DATA, 0xFFFFFFFF),
    ],
)
def test_bad_tcp_header_reconnects_without_reading_body(monkeypatch, bad_header):
    client, sockets = _run_stream(
        monkeypatch,
        [
            [bad_header, b"body must not be read"],
            [DataFrame.from_payload(_payload(1)).encode()],
        ],
    )
    assert sockets[0].reads == 1
    assert client.stats["malformed"] == 1
    assert client.stats["received"] == 1
    assert client.drain()[0].frame.payload == _payload(1)


def test_disconnect_discards_partial_frame(monkeypatch):
    client, _ = _run_stream(
        monkeypatch,
        [
            [DataFrame.from_payload(_payload(1)).encode()[:30], b""],
            [DataFrame.from_payload(_payload(2)).encode()],
        ],
    )
    assert [sample.frame.payload.sequence for sample in client.drain()] == [2]
    assert client.stats["received"] == 1


def test_sequence_filter_after_frame_decode(monkeypatch):
    chunk = b"".join(
        DataFrame.from_payload(_payload(sequence)).encode()
        for sequence in [1, 3, 2, 3, 4]
    )
    client, _ = _run_stream(monkeypatch, [[chunk]])
    assert [sample.frame.payload.sequence for sample in client.drain()] == [1, 3, 4]
    assert client.stats["out_of_order"] == 2
    assert client.stats["sequence_gaps"] == 1


def test_tcp_socketpair(monkeypatch):
    receiver, sender = socket.socketpair()
    client = Client(transport="tcp")
    client._ready = threading.Event()
    monkeypatch.setattr(
        "py_mx_eye.py_mx_eye.socket.create_connection", lambda *args: receiver
    )
    thread = threading.Thread(target=client._receive)
    thread.start()
    try:
        assert client._ready.wait(2)
        sender.sendall(
            DataFrame.from_payload(_payload(1)).encode()
            + DataFrame.from_payload(_payload(2)).encode()
        )
        deadline = time.monotonic() + 2
        while client.stats["received"] < 2 and time.monotonic() < deadline:
            client._stop.wait(0.005)
        assert client.stats["received"] == 2
        assert [sample.frame.payload for sample in client.drain()] == [
            _payload(1),
            _payload(2),
        ]
    finally:
        client._stop.set()
        thread.join(timeout=2)
        sender.close()
        receiver.close()
    assert not thread.is_alive()
    assert client.stats["error"] == ""


def test_udp_drops_bad_datagrams(monkeypatch):
    client = Client(transport="udp")
    client._ready = threading.Event()
    datagrams = deque(
        [
            b"broken",
            HEADER.pack(b"MXEY", MessageType.CMD, 0),
            DataFrame.from_payload(_payload(1)).encode()[:-1],
            DataFrame.from_payload(_payload(1)).encode() + b"extra",
            DataFrame.from_payload(_payload(1)).encode(),
        ]
    )

    class DatagramSocket:
        def setsockopt(self, *args):
            pass

        def bind(self, address):
            pass

        def settimeout(self, timeout):
            pass

        def recvfrom(self, size):
            if datagrams:
                return datagrams.popleft(), ("127.0.0.1", 5556)
            client._stop.set()
            raise TimeoutError

        def close(self):
            pass

    monkeypatch.setattr(
        "py_mx_eye.py_mx_eye.socket.socket", lambda *args: DatagramSocket()
    )
    client._receive()
    assert client.stats["error"] == ""
    assert client.stats["malformed"] == 4
    assert client.stats["received"] == 1
    assert client.drain()[0].frame.payload == _payload(1)
