"""Real ZeroMQ message boundaries, broadcast, session policy and reconnects."""

import time
from contextlib import contextmanager

import pytest
import zmq
from mx_eye.transport import Publisher
from py_mx_eye.receiver import ReceiverConfig, SampleReceiver
from test_sdk_read import _frame, _free_port


def ready(publisher, receiver, session=1):
    """Probe the actual subscription, without a fixed slow-joiner sleep."""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        publisher.send(_frame(0, session=session))
        if receiver.next_sample(0.02) is not None:
            return
    pytest.fail("Subscriber did not receive the readiness probe")


@contextmanager
def stream():
    port = _free_port()
    publisher = Publisher("127.0.0.1", port)
    receiver = SampleReceiver(ReceiverConfig("127.0.0.1", port))
    try:
        receiver.open()
        ready(publisher, receiver)
        yield publisher, receiver, port
    finally:
        receiver.close()
        publisher.close()


def test_messages_are_not_split_or_coalesced():
    with stream() as (publisher, receiver, _):
        for sequence in range(1, 4):
            publisher.send(_frame(sequence))
        assert [receiver.next_sample(1).frame.sequence for _ in range(3)] == [
            1,
            2,
            3,
        ]
        assert receiver.next_sample(0) is None


def test_bad_messages_and_multipart_are_dropped_without_losing_next_message():
    with stream() as (publisher, receiver, _):
        for data in (b"", b"bad", _frame(1)[:-1], _frame(1) + b"x", _frame(1) * 2):
            publisher.send(data)
        publisher._sock.send_multipart([_frame(1), _frame(2)])
        publisher.send(_frame(3))
        assert receiver.next_sample(1).frame.sequence == 3
        assert receiver.next_sample(0) is None


def test_duplicate_out_of_order_and_old_sessions_are_skipped():
    with stream() as (publisher, receiver, _):
        for session, sequence in [(1, 2), (1, 2), (1, 1), (2, 1), (1, 3), (2, 2)]:
            publisher.send(_frame(sequence, session=session))
        observed = [receiver.next_sample(1).frame for _ in range(3)]
        assert [(p.session, p.sequence) for p in observed] == [(1, 2), (2, 1), (2, 2)]
        assert receiver.next_sample(0) is None


def test_multiple_subscribers_receive_the_same_sample():
    with stream() as (publisher, receiver, port):
        second = SampleReceiver(ReceiverConfig("127.0.0.1", port))
        try:
            second.open()
            ready(publisher, second)
            publisher.send(_frame(1))
            assert receiver.next_sample(1).frame == second.next_sample(1).frame
        finally:
            second.close()


def test_subscriber_connects_before_start_and_follows_publisher_restart():
    port = _free_port()
    receiver = SampleReceiver(ReceiverConfig("127.0.0.1", port))
    receiver.open()
    try:
        assert receiver.next_sample(0.02) is None
        for session in (1, 2):
            publisher = Publisher("127.0.0.1", port)
            try:
                ready(publisher, receiver, session=session)
                publisher.send(_frame(1, session=session))
                assert receiver.next_sample(1).frame.session == session
            finally:
                publisher.close()
    finally:
        receiver.close()
    receiver.close()
    with pytest.raises(RuntimeError, match="closed"):
        receiver.next_sample(0)


def test_slow_subscriber_does_not_block_publisher_or_other_subscriber():
    with stream() as (publisher, slow, port):
        fast = SampleReceiver(ReceiverConfig("127.0.0.1", port))
        try:
            fast.open()
            ready(publisher, fast)
            assert publisher._sock.getsockopt(zmq.SNDHWM) == 64
            assert slow._sock.getsockopt(zmq.RCVHWM) == 64
            started = time.monotonic()
            received = []
            # Never drain the slow subscriber. Continue servicing the other one.
            for sequence in range(1, 10001):
                assert publisher.send(_frame(sequence)) == 0
                sample = fast.next_sample(0)
                if sample is not None:
                    received.append(sample.frame.sequence)
            assert time.monotonic() - started < 5
            assert received and received == sorted(set(received))
            # Eventual delivery after the burst, without assuming which messages dropped.
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                publisher.send(_frame(10001))
                sample = fast.next_sample(0.01)
                if sample is not None and sample.frame.sequence == 10001:
                    break
            else:
                pytest.fail("Fast subscriber stopped receiving")
        finally:
            fast.close()


def test_publisher_without_subscribers_and_bind_failure_release_resources():
    port = _free_port()
    publisher = Publisher("127.0.0.1", port)
    try:
        for sequence in range(100):
            assert publisher.send(_frame(sequence)) == 0
        with pytest.raises(zmq.ZMQError):
            Publisher("127.0.0.1", port)
    finally:
        publisher.close()
    replacement = Publisher("127.0.0.1", port)
    replacement.close()
