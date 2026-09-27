"""Exercise stream framing, session filtering and datagram rejection.

The receive loop is driven directly through SampleReceiver with injected socket
sources; the end-to-end test uses a real Publisher, ControlServer and Client.
"""

import socket
import struct
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import replace

import pytest
from mx_eye.control_server import ControlServer
from mx_eye.transport import Publisher
from mx_eye_protocol import DataFrame, MessageType, TrackingFlags, TrackingPayload
from mx_eye_protocol.control import ClockSync as SyncReply
from mx_eye_protocol.control import (
    Command,
    NetworkStatus,
    Reply,
    StatusSnapshot,
    Transport,
)
from py_mx_eye import Client
from py_mx_eye.control import ClockState
from py_mx_eye.receiver import SampleReceiver

HEADER = struct.Struct("<4sBI")


def _no_clock() -> ClockState:
    """A receiver whose clock has never been synchronized."""
    return ClockState()


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


def _fresh_payload(sequence: int, flags: TrackingFlags = TrackingFlags.VALID):
    """A payload stamped now, as a live tracker would send it."""
    stamp = time.perf_counter_ns()
    return replace(_payload(sequence), acquisition_ns=stamp, send_ns=stamp, flags=flags)


class ScriptedSocket:
    """Supply exact recv boundaries and end the loop when its chunks run out."""

    def __init__(
        self, chunks: Iterable[bytes], on_exhausted: Callable[[], None]
    ) -> None:
        self.chunks = deque(chunks)
        self.on_exhausted = on_exhausted
        self.closed = False
        self.reads = 0

    def settimeout(self, timeout: float) -> None:
        pass

    def recv(self, size: int) -> bytes:
        self.reads += 1
        if self.chunks:
            return self.chunks.popleft()
        self.on_exhausted()
        raise TimeoutError

    def close(self) -> None:
        self.closed = True


def _scripted_receiver(
    chunk_groups: Iterable[Iterable[bytes]], max_samples: int = 4096
) -> tuple[SampleReceiver, list[ScriptedSocket]]:
    """Run one synchronous TCP receive loop over scripted chunk boundaries."""
    holder: dict[str, SampleReceiver] = {}
    connections = deque(
        ScriptedSocket(chunks, lambda: holder["receiver"].stop())
        for chunks in chunk_groups
    )
    sockets: list[ScriptedSocket] = []

    def connect(address: tuple[str, int], timeout: float) -> ScriptedSocket:
        assert connections, "the receiver reconnected more often than scripted"
        sock = connections.popleft()
        sockets.append(sock)
        return sock

    receiver = SampleReceiver(
        host="127.0.0.1",
        port=5556,
        udp_bind="0.0.0.0",
        max_samples=max_samples,
        clock_state=_no_clock,
        connect=connect,
    )
    holder["receiver"] = receiver
    receiver.run(Transport.TCP)
    return receiver, sockets


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
def test_tcp_chunk_boundaries(chunks):
    receiver, _ = _scripted_receiver([chunks])
    samples = receiver.drain()
    assert [sample.frame.payload.sequence for sample in samples] == [1, 2]
    assert samples[0].frame.magic == b"MXEY"
    assert samples[0].frame.message_type is MessageType.DATA
    assert samples[0].frame.length == 97
    assert receiver.latest() is samples[-1]
    assert receiver.drain() == []
    assert receiver.counters().malformed == 0
    assert receiver.error == ""


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
def test_bad_tcp_header_reconnects_without_reading_body(bad_header):
    receiver, sockets = _scripted_receiver(
        [
            [bad_header, b"body must not be read"],
            [DataFrame.from_payload(_payload(1)).encode()],
        ]
    )
    assert sockets[0].reads == 1
    assert sockets[0].closed
    assert receiver.counters().malformed == 1
    assert receiver.counters().received == 1
    assert receiver.drain()[0].frame.payload == _payload(1)


def test_disconnect_discards_partial_frame():
    receiver, _ = _scripted_receiver(
        [
            [DataFrame.from_payload(_payload(1)).encode()[:30], b""],
            [DataFrame.from_payload(_payload(2)).encode()],
        ]
    )
    assert [sample.frame.payload.sequence for sample in receiver.drain()] == [2]
    assert receiver.counters().received == 1


def test_a_frame_before_a_bad_header_is_still_delivered():
    """One read may hold a good frame and then lose the stream boundary."""
    receiver, sockets = _scripted_receiver(
        [
            [
                DataFrame.from_payload(_payload(1)).encode()
                + HEADER.pack(b"FAIL", MessageType.DATA, 97)
            ],
            [DataFrame.from_payload(_payload(2)).encode()],
        ]
    )
    assert sockets[0].reads == 1
    assert [sample.frame.payload.sequence for sample in receiver.drain()] == [1, 2]
    assert receiver.counters().received == 2
    assert receiver.counters().malformed == 1
    assert receiver.error == ""


def test_sequence_filter_after_frame_decode():
    chunk = b"".join(
        DataFrame.from_payload(_payload(sequence)).encode()
        for sequence in [1, 3, 2, 3, 4]
    )
    receiver, _ = _scripted_receiver([[chunk]])
    assert [sample.frame.payload.sequence for sample in receiver.drain()] == [1, 3, 4]
    assert receiver.counters().out_of_order == 2
    assert receiver.counters().sequence_gaps == 1


def test_acquisition_skips_count_frames_the_tracker_never_processed():
    chunk = b"".join(
        DataFrame.from_payload(replace(_payload(sequence), frame=frame)).encode()
        for sequence, frame in [(1, 1), (2, 5), (5, 9)]
    )
    receiver, _ = _scripted_receiver([[chunk]])
    assert [sample.frame.payload.sequence for sample in receiver.drain()] == [1, 2, 5]
    assert receiver.counters().sequence_gaps == 2
    assert receiver.counters().acquisition_skips == 4


def test_buffer_overwrites_count_dropped_samples():
    chunk = b"".join(
        DataFrame.from_payload(_payload(sequence)).encode() for sequence in [1, 2, 3, 4]
    )
    receiver, _ = _scripted_receiver([[chunk]], max_samples=2)
    assert [sample.frame.payload.sequence for sample in receiver.drain()] == [3, 4]
    assert receiver.counters().buffer_overwrites == 2
    assert receiver.counters().received == 4


def test_new_session_resets_the_sequence_filter():
    chunk = b"".join(
        DataFrame.from_payload(replace(_payload(sequence), session=session)).encode()
        for session, sequence in [(1, 1), (1, 2), (2, 1), (1, 3)]
    )
    receiver, _ = _scripted_receiver([[chunk]])
    # The session change resets sequence tracking and drops the old buffer.
    assert [
        (sample.frame.payload.session, sample.frame.payload.sequence)
        for sample in receiver.drain()
    ] == [(2, 1)]
    assert receiver.counters().received == 3
    assert receiver.counters().out_of_order == 0


def test_tcp_socketpair():
    receiver_end, sender = socket.socketpair()
    receiver = SampleReceiver(
        host="127.0.0.1",
        port=5556,
        udp_bind="0.0.0.0",
        max_samples=4096,
        clock_state=_no_clock,
        connect=lambda address, timeout: receiver_end,
    )
    thread = threading.Thread(target=receiver.run, args=(Transport.TCP,))
    thread.start()
    try:
        assert receiver.wait_ready(2)
        sender.sendall(
            DataFrame.from_payload(_payload(1)).encode()
            + DataFrame.from_payload(_payload(2)).encode()
        )
        deadline = time.monotonic() + 2
        while receiver.counters().received < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert receiver.counters().received == 2
        assert [sample.frame.payload for sample in receiver.drain()] == [
            _payload(1),
            _payload(2),
        ]
    finally:
        receiver.stop()
        thread.join(timeout=2)
        sender.close()
    assert not thread.is_alive()
    assert receiver.error == ""


def test_udp_drops_bad_datagrams():
    holder: dict[str, SampleReceiver] = {}
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
            holder["receiver"].stop()
            raise TimeoutError

        def close(self):
            pass

    receiver = SampleReceiver(
        host="127.0.0.1",
        port=5556,
        udp_bind="0.0.0.0",
        max_samples=4096,
        clock_state=_no_clock,
        datagram=lambda *args: DatagramSocket(),
    )
    holder["receiver"] = receiver
    receiver.run(Transport.UDP)
    assert receiver.error == ""
    assert receiver.counters().malformed == 4
    assert receiver.counters().received == 1
    assert receiver.drain()[0].frame.payload == _payload(1)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _deliver(publisher: Publisher, client: Client, frame: bytes, target: int) -> bool:
    """Publish until the receiver has counted ``target`` samples."""

    def attempt() -> bool:
        publisher.send(frame)
        return client.stats.received >= target

    return _wait_until(attempt)


def test_client_receives_published_frames():
    """The public Client path: connect, receive, then apply the freshness rules."""
    data_port, control_port = _free_port(), _free_port()

    def dispatch(request, received_ns):
        if request.command is Command.SYNC:
            return Reply(
                sync=SyncReply(t1=request.t1, t2=received_ns, t3=time.perf_counter_ns())
            )
        return Reply(
            status=StatusSnapshot(
                state="idle",
                network=NetworkStatus(
                    data_port=data_port,
                    control_port=control_port,
                    transport=Transport.TCP,
                ),
            )
        )

    with ControlServer(("127.0.0.1", control_port), dispatch) as server:
        server_thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        server_thread.start()
        publisher = Publisher("127.0.0.1", data_port)
        client = Client("127.0.0.1", data_port, control_port)
        try:
            assert client.connect() is client
            assert _wait_until(lambda: client.stats.clock_synced)

            fresh = DataFrame.from_payload(_fresh_payload(1)).encode()
            assert _deliver(publisher, client, fresh, 1)
            sample = client.latest(max_age_ms=50)
            assert sample is not None
            assert sample.frame.payload.x == 1.0

            stale = DataFrame.from_payload(
                replace(
                    _fresh_payload(2),
                    acquisition_ns=time.perf_counter_ns() - 5_000_000_000,
                )
            ).encode()
            assert _deliver(publisher, client, stale, 2)
            assert client.latest(max_age_ms=50) is None
            assert client.latest(max_age_ms=None, require_valid=False) is not None

            lost = DataFrame.from_payload(
                _fresh_payload(3, flags=TrackingFlags.NONE)
            ).encode()
            assert _deliver(publisher, client, lost, 3)
            assert client.latest(max_age_ms=None) is None
            assert client.latest(max_age_ms=None, require_valid=False) is not None

            assert [s.frame.payload.sequence for s in client.drain()] == [1, 2, 3]
            assert client.drain() == []
            assert client.stats.clock_synced
            assert client.stats.error == ""
            assert client.transport is Transport.TCP
        finally:
            client.close()
            publisher.close()
            server.shutdown()
            server_thread.join(timeout=2)
    assert not server_thread.is_alive()
