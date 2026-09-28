"""Public SDK handle: one synchronous connection to one tracker.

No Qt or OpenCV dependency, and no thread of its own: every call runs on the
caller's thread, so a consumer that owns a thread stays in control of it.

- ``with MxEye(...)`` scopes the sample stream: ``connect()`` on entry,
  ``close()`` on exit.
- ``read()`` iterates the samples that are still current measurements; the
  handle decides freshness and validity, so a caller never receives a lost or
  stale point as if it were current.
- ``start()``/``stop()``/``status()`` drive the tracker's control port and never
  touch the sample stream, so acquisition is only ever started explicitly.
"""

import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Self

from mx_eye_protocol.control import Command, StatusSnapshot, Transport

from .control import ControlClient
from .receiver import ReceiverConfig, SampleReceiver
from .sample import Sample


@dataclass(frozen=True)
class MxEyeConfig:
    """How to reach one tracker.

    This is the constructor parameter object, not the tracker's own JSON
    configuration document. ``transport`` is the enum the tracker publishes on
    (default TCP); it is stated here rather than inferred from the tracker, so it
    must match that tracker's own setting. ``timeout`` bounds the socket connect
    and the control requests.
    """

    host: str = "127.0.0.1"
    data_port: int = 5556
    control_port: int = 5557
    transport: Transport = Transport.TCP
    udp_bind: str = "0.0.0.0"
    timeout: float = 3.0


class MxEye:
    """Synchronous receiver for one tracker, with an explicit lifecycle."""

    def __init__(self, config: MxEyeConfig | None = None) -> None:
        self.config = config if config is not None else MxEyeConfig()
        self._connected = False
        self._control = ControlClient(
            self.config.host, self.config.control_port, self.config.timeout
        )
        self._receiver = SampleReceiver(
            ReceiverConfig(
                host=self.config.host,
                port=self.config.data_port,
                udp_bind=self.config.udp_bind,
            )
        )

    def connect(self) -> Self:
        """Open the sample stream, raising when the tracker is not publishing.

        The tracker's data port exists only while a tracking session runs, so
        connect after starting one (`start()`, or the tracker's own Start).
        Nothing is retried here; a caller that wants to wait reconnects.
        """
        self._receiver.open(self.config.transport, self.config.timeout)
        self._connected = True
        return self

    def read(
        self,
        timeout: float | None = None,
        max_age_ms: float | None = 50.0,
        require_valid: bool = True,
    ) -> Iterator[Sample]:
        """Yield the samples that are current measurements, as they arrive.

        ``for sample in eye.read():`` blocks on the caller's thread for each
        sample, so a task loop never needs a sleep. ``timeout`` bounds each wait
        for the next sample rather than the whole loop, and the loop ends when a
        wait expires with nothing usable: ``read(0)`` is a non-blocking drain of
        what has already arrived, a finite ``timeout`` also ends the loop after
        that much silence, and the default waits indefinitely.

        Lost, stale and validity-failing samples are skipped while waiting, so
        every yielded sample is a current measurement; a stream that keeps
        arriving but is never usable therefore keeps the loop waiting.
        ``max_age_ms=None`` skips the age check and ``require_valid=False`` skips
        the validity check, which together yield whatever arrives next: that is
        what a plot or a diagnostics readout wants.
        """
        if not self._connected:
            raise RuntimeError("Connect before reading")
        return self._iter_samples(timeout, max_age_ms, require_valid)

    def _iter_samples(
        self,
        timeout: float | None,
        max_age_ms: float | None,
        require_valid: bool,
    ) -> Iterator[Sample]:
        while True:
            sample = self._receiver.next_sample(timeout)
            if sample is None:
                return
            if sample.is_fresh_at(time.time_ns(), max_age_ms, require_valid):
                yield sample

    def start(self) -> StatusSnapshot:
        """Start acquisition on the tracker; the sample stream is untouched."""
        return self._control.request_status(
            Command.START, timeout=max(20.0, self.config.timeout)
        )

    def stop(self) -> StatusSnapshot:
        """Stops acquisition; status() reports when video draining is complete."""
        return self._control.request_status(Command.STOP)

    def status(self) -> StatusSnapshot:
        return self._control.request_status(Command.STATUS)

    def close(self) -> None:
        """Release the sample stream. Does not stop the tracker."""
        self._receiver.close()
        self._connected = False

    def __enter__(self) -> Self:
        return self.connect()

    def __exit__(self, *args: object) -> None:
        self.close()
