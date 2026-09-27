"""Public receiver SDK: composes control access, clock sync and reception.

No Qt or OpenCV dependency. ``Client`` receives continuously even when the
caller is busy drawing; ``latest()`` never returns a stale point as if it were
current, and ``drain()`` exposes the bounded buffer for plotting or logging.
"""

import time
from collections.abc import Callable
from typing import Self

from mx_eye_protocol.control import Command, StatusSnapshot, Transport
from pydantic import BaseModel, ConfigDict

from .control import ClockSync, ControlClient
from .receiver import SampleReceiver
from .sample import Sample


class Stats(BaseModel):
    """Receiver counters plus live clock-synchronization state.

    Read on demand rather than per frame, so this is a validated model. NaN is
    allowed because ``sync_rtt_ms`` stays NaN until the first synchronization.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    received: int
    sequence_gaps: int
    acquisition_skips: int
    malformed: int
    out_of_order: int
    buffer_overwrites: int
    error: str
    sync_rtt_ms: float
    clock_synced: bool


class Client:
    """Thread-based receiver for one tracker, with an explicit lifecycle."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        data_port: int = 5556,
        control_port: int = 5557,
        transport: Transport | str | None = None,
        udp_bind: str = "0.0.0.0",
        buffer_samples: int = 4096,
        timeout: float = 3.0,
        *,
        now: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        self.host, self.data_port = host, data_port
        self.control_port = control_port
        self.transport = Transport(transport) if transport is not None else None
        self.udp_bind = udp_bind
        self.timeout = timeout
        self._now = now
        self._connected = False
        self._control = ControlClient(host, control_port, timeout, now=now)
        self._clock = ClockSync(self._control, now=now)
        self._receiver = SampleReceiver(
            host=host,
            port=data_port,
            udp_bind=udp_bind,
            max_samples=buffer_samples,
            clock_state=self._clock.state,
            now=now,
        )

    def connect(self) -> Self:
        """Resolve the transport, start receiving and wait until it is usable."""
        if self._connected:
            return self
        status = self.status()
        self._receiver.reset_error()
        self.transport = self.transport or status.network.transport
        self._receiver.start(self.transport)
        self._clock.start()
        if not self._receiver.wait_ready(self.timeout):
            self.close()
            raise TimeoutError("Receiver socket did not become ready")
        error = self._receiver.error
        if error:
            self.close()
            raise RuntimeError(error)
        self._connected = True
        return self

    def start(self) -> StatusSnapshot:
        self.connect()
        return self._control.request_status(
            Command.START, timeout=max(20.0, self.timeout)
        )

    def stop(self) -> StatusSnapshot:
        """Stops acquisition; status() reports when video draining is complete."""
        return self._control.request_status(Command.STOP)

    def status(self) -> StatusSnapshot:
        return self._control.request_status(Command.STATUS)

    def latest(
        self, max_age_ms: float | None = 50.0, require_valid: bool = True
    ) -> Sample | None:
        """The newest sample, or None when it is invalid, lost or stale."""
        sample = self._receiver.latest()
        if sample is None or not sample.is_fresh_at(
            self._now(), max_age_ms, require_valid
        ):
            return None
        return sample

    def drain(self) -> list[Sample]:
        """Return and clear buffered samples. Receiving never waits for this."""
        return self._receiver.drain()

    @property
    def stats(self) -> Stats:
        counters = self._receiver.counters()
        return Stats(
            received=counters.received,
            sequence_gaps=counters.sequence_gaps,
            acquisition_skips=counters.acquisition_skips,
            malformed=counters.malformed,
            out_of_order=counters.out_of_order,
            buffer_overwrites=counters.buffer_overwrites,
            error=counters.error,
            sync_rtt_ms=self._clock.rtt_ms,
            clock_synced=self._clock.synced,
        )

    def close(self) -> None:
        self._receiver.stop()
        self._clock.stop()
        self._connected = False

    def __enter__(self) -> Self:
        return self.connect()

    def __exit__(self, *args: object) -> None:
        self.close()
