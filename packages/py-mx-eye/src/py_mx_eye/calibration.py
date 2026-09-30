"""Synchronous calibration event submission and explicit retry state."""

import threading
from uuid import UUID

from mx_eye_protocol.calibration import (
    CalibrationAck,
    CalibrationConfig,
    CalibrationDataFrame,
    CalibrationState,
    CalibrationStatus,
    CalibrationStimulus,
    CalibrationStop,
)
from mx_eye_protocol.control import Command, ControlReply, ControlRequest

from ._rpc import request_reply
from .control import ControlClient


class CalibrationClient:
    """One data REQ socket per call; successful ACKs must identify the frame."""

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    def send(self, frame: CalibrationDataFrame) -> CalibrationAck:
        ack = CalibrationAck.model_validate_json(
            request_reply(self.host, self.port, self.timeout, frame.encode())
        )
        if ack.calibration_id != frame.calibration_id or ack.sequence != frame.sequence:
            raise ValueError("Calibration ACK does not match the submitted frame")
        if not ack.ok:
            raise RuntimeError(ack.error or "Tracker rejected calibration frame")
        return ack


def calibration_result(
    reply: ControlReply, calibration_id: UUID, state: CalibrationState
) -> CalibrationStatus:
    """Require the requested calibration result, rather than a generic status."""
    result = reply.status.calibration if reply.status is not None else None
    if (
        result is None
        or result.calibration_id != calibration_id
        or result.state is not state
    ):
        raise ValueError("Control reply does not match the requested calibration state")
    return result


class CalibrationSession:
    """One calibration session, used on its creating thread.

    Only acknowledged sends advance the sequence. After an uncertain send,
    explicitly send the identical stimulus again; no implicit retry occurs.
    Timestamps are preserved verbatim, trusting external chrony synchronization.
    """

    def __init__(
        self,
        config: CalibrationConfig,
        status: CalibrationStatus,
        control: ControlClient,
        data: CalibrationClient,
    ) -> None:
        self._config = config.model_copy(deep=True)
        self._status = status
        self._control = control
        self._data = data
        self._pending: CalibrationDataFrame | None = None
        self._stopping = False
        self._closed = False
        self._thread = threading.get_ident()

    @property
    def calibration_id(self) -> UUID:
        return self._config.calibration_id

    @property
    def status(self) -> CalibrationStatus:
        """Last confirmed local state; use MxEye.status() for remote recovery."""
        return self._status

    @property
    def finished(self) -> bool:
        return self._closed or self._status.state is CalibrationState.STOPPED

    def _check_open(self) -> None:
        if threading.get_ident() != self._thread:
            raise RuntimeError("Use a calibration session on its creating thread")
        if self._closed:
            raise RuntimeError("Calibration session is closed")

    def send(self, stimulus: CalibrationStimulus) -> CalibrationAck:
        """Submit one presentation event and wait for acceptance."""
        self._check_open()
        if self._stopping or self._status.state is CalibrationState.STOPPED:
            raise RuntimeError("Cannot send to a stopping or stopped calibration")
        frame = CalibrationDataFrame(
            calibration_id=self.calibration_id,
            sequence=self._status.last_sequence + 1,
            stimulus=stimulus,
        )
        if self._pending is not None and frame != self._pending:
            raise RuntimeError("Retry the pending stimulus before sending another")
        self._pending = frame
        try:
            ack = self._data.send(frame)
        except RuntimeError:
            # An explicit rejection confirms that this frame was not accepted.
            self._pending = None
            raise
        self._status = CalibrationStatus(
            calibration_id=self.calibration_id,
            state=CalibrationState.ACTIVE,
            last_sequence=ack.sequence,
        )
        self._pending = None
        return ack

    def stop(self) -> CalibrationStatus:
        """Finish after all submitted events are confirmed; explicit retry is safe."""
        self._check_open()
        if self._status.state is CalibrationState.STOPPED:
            return self._status
        if self._pending is not None:
            raise RuntimeError("Confirm the pending stimulus before stopping")
        request = ControlRequest(
            command=Command.CALIBRATION_STOP,
            calibration_stop=CalibrationStop(
                calibration_id=self.calibration_id,
                final_sequence=self._status.last_sequence,
            ),
        )
        self._stopping = True
        try:
            reply = self._control.rpc(request)
        except RuntimeError:
            self._stopping = False
            raise
        result = calibration_result(
            reply, self.calibration_id, CalibrationState.STOPPED
        )
        if result.last_sequence != self._status.last_sequence:
            raise ValueError("Stop reply does not confirm the final sequence")
        self._status = result
        return result

    def close(self) -> None:
        """Local invalidation only; never issue a remote stop implicitly."""
        self._closed = True
