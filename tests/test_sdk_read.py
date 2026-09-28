"""The public synchronous read path: freshness, validity and session scope.

Driven through ``MxEye`` against a real Publisher and ControlServer, so these
tests cover what the SDK promises a consumer rather than the framing internals.
"""

import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest
from mx_eye.control_server import ControlServer
from mx_eye.transport import Publisher
from mx_eye_protocol import DataFrame, TrackingFlags, TrackingPayload
from mx_eye_protocol.control import NetworkStatus, Reply, StatusSnapshot, Transport
from py_mx_eye import MxEye, MxEyeConfig


def _payload(sequence: int, session: int = 1) -> TrackingPayload:
    """A valid payload stamped now, as a live tracker would send it."""
    stamp = time.time_ns()
    return TrackingPayload(
        session=session,
        sequence=sequence,
        frame=sequence,
        acquisition_ns=stamp,
        tracking_start_ns=stamp,
        tracking_end_ns=stamp,
        send_ns=stamp,
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


def _frame(
    sequence: int,
    session: int = 1,
    age_ns: int = 0,
    flags: TrackingFlags = TrackingFlags.VALID,
) -> bytes:
    """One encoded frame, optionally aged or invalidated."""
    stamp = time.time_ns() - age_ns
    payload = _payload(sequence, session).model_copy(
        update={"acquisition_ns": stamp, "send_ns": stamp, "flags": flags}
    )
    return DataFrame.from_payload(payload).encode()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@contextmanager
def running_tracker() -> Iterator[tuple[MxEyeConfig, Publisher]]:
    """A tracker end with a live data port, ready for MxEye to connect to."""
    data_port, control_port = _free_port(), _free_port()

    def dispatch(request):
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
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        thread.start()
        publisher = Publisher("127.0.0.1", data_port)
        config = MxEyeConfig(
            host="127.0.0.1", data_port=data_port, control_port=control_port
        )
        try:
            yield config, publisher
        finally:
            publisher.close()
            server.shutdown()
            thread.join(timeout=2)


def _send(publisher: Publisher, frame: bytes) -> None:
    """Send one frame once the tracker has accepted this SDK connection.

    ``Publisher.send`` accepts pending connections before it writes, so a send
    that leaves the client list non-empty has delivered the frame.
    """

    def delivered() -> bool:
        publisher.send(frame)
        return bool(publisher.clients)

    assert _wait_until(delivered), "the tracker never accepted the SDK connection"


def test_read_yields_a_published_sample():
    with running_tracker() as (config, publisher), MxEye(config) as eye:
        _send(publisher, _frame(1))
        samples = list(eye.read(timeout=0.2))
        assert [sample.frame.payload.sequence for sample in samples] == [1]
        assert samples[0].frame.payload.x == 1.0
        assert eye.config.transport is Transport.TCP


def test_read_yields_every_sample_that_arrived():
    with running_tracker() as (config, publisher), MxEye(config) as eye:
        _send(publisher, _frame(1))
        for sequence in (2, 3):
            publisher.send(_frame(sequence))
        assert [sample.frame.payload.sequence for sample in eye.read(timeout=0.2)] == [
            1,
            2,
            3,
        ]


def test_read_ends_the_loop_when_nothing_arrives():
    with running_tracker() as (config, _publisher), MxEye(config) as eye:
        started = time.monotonic()
        assert list(eye.read(timeout=0.05)) == []
        assert time.monotonic() - started >= 0.03


def test_read_with_zero_timeout_drains_without_blocking():
    with running_tracker() as (config, publisher), MxEye(config) as eye:
        assert list(eye.read(timeout=0.0)) == []
        _send(publisher, _frame(1))
        assert [sample.frame.payload.sequence for sample in eye.read(timeout=0.0)] == [
            1
        ]


def test_a_stale_sample_is_skipped_and_reading_continues():
    with running_tracker() as (config, publisher), MxEye(config) as eye:
        _send(publisher, _frame(1, age_ns=5_000_000_000))
        # The stale sample is consumed and discarded, not yielded.
        assert list(eye.read(timeout=0.05)) == []
        publisher.send(_frame(2))
        samples = list(eye.read(timeout=0.2))
        assert [sample.frame.payload.sequence for sample in samples] == [2]


def test_a_clock_that_disagrees_is_not_certified_fresh():
    with running_tracker() as (config, publisher), MxEye(config) as eye:
        _send(publisher, _frame(1, age_ns=-5_000_000_000))
        assert list(eye.read(timeout=0.05)) == []
        publisher.send(_frame(2, age_ns=-5_000_000_000))
        samples = list(eye.read(timeout=0.2, max_age_ms=None, require_valid=False))
        assert len(samples) == 1 and samples[0].age_ms < 0


def test_an_invalid_sample_is_skipped_by_default():
    with running_tracker() as (config, publisher), MxEye(config) as eye:
        _send(publisher, _frame(1, flags=TrackingFlags.NONE))
        assert list(eye.read(timeout=0.05)) == []
        publisher.send(_frame(2, flags=TrackingFlags.NONE))
        samples = list(eye.read(timeout=0.2, require_valid=False))
        assert len(samples) == 1 and not samples[0].frame.payload.valid


def test_diagnostic_mode_yields_whatever_arrives():
    with running_tracker() as (config, publisher), MxEye(config) as eye:
        _send(publisher, _frame(1, age_ns=5_000_000_000))
        samples = list(eye.read(timeout=0.2, max_age_ms=None, require_valid=False))
        assert [sample.frame.payload.sequence for sample in samples] == [1]
        assert samples[0].age_ms > 1000


def test_a_new_session_is_read():
    with running_tracker() as (config, publisher), MxEye(config) as eye:
        _send(publisher, _frame(1))
        assert [s.frame.payload.session for s in eye.read(timeout=0.2)] == [1]
        publisher.send(_frame(1, session=2))
        samples = list(eye.read(timeout=0.2))
        assert [
            (sample.frame.payload.session, sample.frame.payload.sequence)
            for sample in samples
        ] == [(2, 1)]


def test_leaving_the_with_block_releases_the_stream():
    with running_tracker() as (config, _publisher):
        with MxEye(config) as eye:
            pass
        with pytest.raises(RuntimeError, match="Connect before reading"):
            eye.read(timeout=0.0)


def test_leaving_the_with_block_releases_the_stream_on_error():
    with running_tracker() as (config, _publisher):
        with pytest.raises(RuntimeError, match="task failed"), MxEye(config) as eye:
            raise RuntimeError("task failed")
        with pytest.raises(RuntimeError, match="Connect before reading"):
            eye.read(timeout=0.0)
