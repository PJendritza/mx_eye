"""Synchronous data plane: framing, filtering, timeouts and stream failure.

SampleReceiver is driven directly over real sockets: a socketpair for TCP and a
bound datagram socket for UDP. The public MxEye path is covered in
test_sdk_read.py, and byte-at-a-time framing in test_framing.py.
"""

import socket
import struct
import time

import pytest
from mx_eye_protocol import DataFrame, MessageType, TrackingFlags, TrackingPayload
from mx_eye_protocol.control import Transport
from py_mx_eye.receiver import FrameError, ReceiverConfig, SampleReceiver

HEADER = struct.Struct("<4sBI")


def _payload(
    sequence: int, session: int = 1, source_frame: int | None = None
) -> TrackingPayload:
    return TrackingPayload(
        session=session,
        sequence=sequence,
        frame=sequence if source_frame is None else source_frame,
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


def _frame(*payloads: TrackingPayload) -> bytes:
    return b"".join(DataFrame.from_payload(payload).encode() for payload in payloads)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _tcp_receiver() -> tuple[SampleReceiver, socket.socket]:
    """A receiver reading one end of a connected socketpair."""
    peer, sender = socket.socketpair()
    receiver = SampleReceiver(
        ReceiverConfig(
            host="127.0.0.1",
            port=5556,
            connect=lambda address, timeout: peer,
        )
    )
    receiver.open(Transport.TCP, 1.0)
    return receiver, sender


def _udp_receiver() -> tuple[SampleReceiver, socket.socket, int]:
    """A receiver bound to a datagram port, plus a socket that can send to it."""
    port = _free_port()
    receiver = SampleReceiver(ReceiverConfig(host="127.0.0.1", port=port))
    receiver.open(Transport.UDP, 1.0)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    return receiver, sender, port


def _sequences(receiver: SampleReceiver, timeout: float = 0.05) -> list[int]:
    """Read until nothing arrives within the timeout."""
    seen: list[int] = []
    while (sample := receiver.next_sample(timeout)) is not None:
        seen.append(sample.frame.payload.sequence)
    return seen


def test_one_frame_per_call():
    receiver, sender = _tcp_receiver()
    try:
        sender.sendall(_frame(_payload(1), _payload(2)))
        first = receiver.next_sample(1.0)
        second = receiver.next_sample(1.0)
        assert first is not None and first.frame.payload.sequence == 1
        assert second is not None and second.frame.payload.sequence == 2
        assert first.receive_ns > 0
    finally:
        receiver.close()
        sender.close()


def test_zero_timeout_polls_and_times_out_without_blocking():
    receiver, sender = _tcp_receiver()
    try:
        assert receiver.next_sample(0.0) is None
        started = time.monotonic()
        assert receiver.next_sample(0.05) is None
        assert time.monotonic() - started >= 0.03
        sender.sendall(_frame(_payload(1)))
        assert receiver.next_sample(0.0).frame.payload.sequence == 1
    finally:
        receiver.close()
        sender.close()


def test_a_partial_frame_waits_for_the_rest():
    receiver, sender = _tcp_receiver()
    data = _frame(_payload(1))
    try:
        sender.sendall(data[:40])
        assert receiver.next_sample(0.05) is None
        sender.sendall(data[40:])
        sample = receiver.next_sample(1.0)
        assert sample is not None and sample.frame.payload.sequence == 1
    finally:
        receiver.close()
        sender.close()


def test_duplicates_and_old_sequences_are_dropped():
    receiver, sender = _tcp_receiver()
    try:
        sender.sendall(_frame(*(_payload(s) for s in (1, 3, 2, 3, 4))))
        assert _sequences(receiver) == [1, 3, 4]
    finally:
        receiver.close()
        sender.close()


def test_a_new_session_restarts_the_sequence_filter():
    receiver, sender = _tcp_receiver()
    try:
        sender.sendall(
            _frame(
                _payload(1),
                _payload(2),
                _payload(1, session=2),
                _payload(3),
            )
        )
        samples = []
        while (sample := receiver.next_sample(0.05)) is not None:
            payload = sample.frame.payload
            samples.append((payload.session, payload.sequence))
        # The first session's later sequence is left behind, not replayed.
        assert samples == [(1, 1), (1, 2), (2, 1)]
    finally:
        receiver.close()
        sender.close()


def test_a_good_frame_before_a_bad_header_is_delivered():
    """One read may hold a good frame and then lose the stream boundary."""
    receiver, sender = _tcp_receiver()
    try:
        sender.sendall(_frame(_payload(1)) + HEADER.pack(b"FAIL", MessageType.DATA, 97))
        sample = receiver.next_sample(1.0)
        assert sample is not None and sample.frame.payload.sequence == 1
        with pytest.raises(FrameError):
            receiver.next_sample(1.0)
        # The stream is gone; the caller has to connect again.
        with pytest.raises(RuntimeError, match="connect again"):
            receiver.next_sample(1.0)
    finally:
        receiver.close()
        sender.close()


def test_a_closed_peer_is_a_connection_error():
    receiver, sender = _tcp_receiver()
    try:
        sender.close()
        with pytest.raises(ConnectionError, match="closed the sample stream"):
            receiver.next_sample(1.0)
    finally:
        receiver.close()


def test_udp_drops_bad_datagrams_without_losing_the_stream():
    receiver, sender, port = _udp_receiver()
    good = _frame(_payload(1))
    try:
        for data in (
            b"broken",
            HEADER.pack(b"MXEY", MessageType.CMD, 0),
            good[:-1],
            good + b"extra",
        ):
            sender.sendto(data, ("127.0.0.1", port))
        sender.sendto(good, ("127.0.0.1", port))
        sample = receiver.next_sample(1.0)
        assert sample is not None and sample.frame.payload == _payload(1)
    finally:
        receiver.close()
        sender.close()
