"""Public SDK handle: one synchronous connection to one tracker.

No Qt or OpenCV dependency or Python background receiver thread. Calls run on
the caller's thread; libzmq manages transport I/O internally.

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

from mx_eye_protocol.calibration import CalibrationConfig, CalibrationState
from mx_eye_protocol.control import Command, ControlRequest, StatusSnapshot

from .calibration import CalibrationClient, CalibrationSession, calibration_result
from .control import ControlClient
from .receiver import ReceiverConfig, SampleReceiver
from .sample import Sample


@dataclass(frozen=True)
class MxEyeConfig:
    """How to reach one tracker.

    ``timeout`` bounds control requests; data reads have their own timeout.
    """

    host: str = "127.0.0.1"
    data_port: int = 5556
    control_port: int = 5557
    timeout: float = 3.0
    calibration_port: int = 5558


class MxEye:
    """Synchronous receiver for one tracker, with an explicit lifecycle."""

    def __init__(self, config: MxEyeConfig | None = None) -> None:
        self.config: MxEyeConfig = config if config is not None else MxEyeConfig()
        self._connected = False
        self._control = ControlClient(
            self.config.host, self.config.control_port, self.config.timeout
        )
        self._calibration: CalibrationSession | None = None
        self._starting_calibration: CalibrationConfig | None = None
        self._receiver = SampleReceiver(
            ReceiverConfig(
                host=self.config.host,
                port=self.config.data_port,
            )
        )

    def connect(self) -> Self:
        """Subscribe asynchronously; the tracker may start or restart later."""
        self._receiver.open()
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

    def start_calibration(self, config: CalibrationConfig) -> CalibrationSession:
        """Start stimulus submission, independently of acquisition and read().

        Reuse config after an uncertain start to retry the same calibration ID.
        A tracker must implement calibration protocol 1.1.0 and the data port.
        """
        if self._calibration is not None and not self._calibration.finished:
            raise RuntimeError("A calibration session is already active")
        if (
            self._starting_calibration is not None
            and config != self._starting_calibration
        ):
            raise RuntimeError(
                "Retry the pending calibration start with the same config"
            )
        self._starting_calibration = config.model_copy(deep=True)
        request = ControlRequest(
            command=Command.CALIBRATION_START,
            calibration_start=self._starting_calibration,
        )
        try:
            reply = self._control.rpc(request)
        except RuntimeError:
            self._starting_calibration = None
            raise
        result = calibration_result(
            reply, config.calibration_id, CalibrationState.ACTIVE
        )
        self._calibration = CalibrationSession(
            self._starting_calibration,
            result,
            self._control,
            CalibrationClient(
                self.config.host, self.config.calibration_port, self.config.timeout
            ),
        )
        self._starting_calibration = None
        return self._calibration

    def close(self) -> None:
        """Release the sample stream. Does not stop the tracker."""
        self._receiver.close()
        self._connected = False
        if self._calibration is not None:
            self._calibration.close()
        self._starting_calibration = None

    def __enter__(self) -> Self:
        return self.connect()

    def __exit__(self, *args: object) -> None:
        self.close()
