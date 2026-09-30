"""Real REQ/REP tests against a test-only calibration service.

The service implements acceptance and deduplication, not tracking or calibration.
"""

import concurrent.futures
import dataclasses
import threading
from contextlib import contextmanager
from uuid import uuid4

import pytest
import zmq
from mx_eye_protocol import (
    CalibrationAck,
    CalibrationDataFrame,
    CalibrationState,
    CalibrationStatus,
    Command,
    ControlReply,
    ControlRequest,
    StatusSnapshot,
)
from py_mx_eye import (
    AnimalInfo,
    CalibrationConfig,
    CalibrationCoordinateUnit,
    CalibrationPosition,
    CalibrationSession,
    CalibrationSize,
    CalibrationStimulus,
    MxEye,
    MxEyeConfig,
    ScreenDimensions,
)
from py_mx_eye.calibration import CalibrationClient


@contextmanager
def rep_endpoint(dispatch):
    ready = concurrent.futures.Future()
    shutdown = threading.Event()

    def run():
        with zmq.Context() as context, context.socket(zmq.REP) as sock:
            sock.setsockopt(zmq.LINGER, 0)
            port = sock.bind_to_random_port("tcp://127.0.0.1")
            ready.set_result(port)
            while not shutdown.is_set():
                if sock.poll(20):
                    parts = sock.recv_multipart()
                    assert len(parts) == 1
                    response = dispatch(parts[0])
                    sock.send_multipart(
                        response if isinstance(response, list) else [response]
                    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run)
        try:
            yield ready.result(timeout=2)
        finally:
            shutdown.set()
            future.result(timeout=3)


class CalibrationService:
    def __init__(self):
        self.config = None
        self.state = CalibrationState.ACTIVE
        self.frames = {}
        self.commands = []
        self.data_requests = []
        self.fault = None
        self.hold_command = None
        self.release = threading.Event()
        self.entered = threading.Event()
        self.reject_command = None
        self.bad_control = None
        self.stopped_ids = set()

    def status(self):
        return CalibrationStatus(
            calibration_id=self.config.calibration_id,
            state=self.state,
            last_sequence=len(self.frames),
        )

    def control(self, data):
        request = ControlRequest.model_validate_json(data)
        self.commands.append(request)
        if self.reject_command is request.command:
            self.reject_command = None
            return (
                ControlReply(ok=False, error="Rejected command")
                .model_dump_json()
                .encode()
            )
        if request.command is Command.CALIBRATION_START:
            if request.calibration_start.calibration_id in self.stopped_ids:
                return (
                    ControlReply(ok=False, error="Calibration already stopped")
                    .model_dump_json()
                    .encode()
                )
            if self.config is None or self.state is CalibrationState.STOPPED:
                self.config = request.calibration_start
                self.frames = {}
                self.state = CalibrationState.ACTIVE
            elif request.calibration_start != self.config:
                return (
                    ControlReply(ok=False, error="Another session is active")
                    .model_dump_json()
                    .encode()
                )
        elif request.command is Command.CALIBRATION_STOP:
            stop = request.calibration_stop
            if (
                stop.calibration_id != self.config.calibration_id
                or stop.final_sequence != len(self.frames)
            ):
                return (
                    ControlReply(ok=False, error="Final sequence mismatch")
                    .model_dump_json()
                    .encode()
                )
            self.state = CalibrationState.STOPPED
            self.stopped_ids.add(self.config.calibration_id)
        elif request.command not in (Command.STATUS, Command.START, Command.STOP):
            raise AssertionError("Unexpected command")
        if self.hold_command is request.command:
            self.hold_command = None
            self.entered.set()
            assert self.release.wait(2)
        snapshot = StatusSnapshot(
            state="running",
            calibration=self.status() if self.config is not None else None,
        )
        if self.bad_control is not None:
            snapshot = self.bad_control(snapshot)
            self.bad_control = None
        return ControlReply(status=snapshot).model_dump_json().encode()

    def data(self, data):
        frame = CalibrationDataFrame.decode(data)
        self.data_requests.append(data)
        fault, self.fault = self.fault, None
        error = None
        if fault == "reject":
            error = "Invalid stimulus"
        elif (
            frame.calibration_id != self.config.calibration_id
            or self.state is not CalibrationState.ACTIVE
        ):
            error = "Calibration is not active"
        elif frame.sequence in self.frames:
            if self.frames[frame.sequence] != frame:
                error = "Conflicting retry"
        elif frame.sequence != len(self.frames) + 1:
            error = "Sequence gap"
        else:
            self.frames[frame.sequence] = frame
        ack = CalibrationAck(
            calibration_id=frame.calibration_id,
            sequence=frame.sequence,
            ok=error is None,
            error=error,
        )
        if fault == "timeout":
            self.entered.set()
            assert self.release.wait(2)
        if fault == "wrong_sequence":
            ack = ack.model_copy(update={"sequence": frame.sequence + 1})
        if fault == "wrong_id":
            ack = ack.model_copy(
                update={
                    "calibration_id": CalibrationConfig(
                        animal_info=AnimalInfo(animal_id="other"),
                        screen_dimensions=ScreenDimensions(width=1, height=1),
                    ).calibration_id
                }
            )
        if fault == "malformed":
            return b"invalid json"
        if fault == "multipart":
            return [ack.model_dump_json().encode(), b"extra"]
        return ack.model_dump_json().encode()


@pytest.fixture
def service():
    tracker = CalibrationService()
    with (
        rep_endpoint(tracker.control) as control_port,
        rep_endpoint(tracker.data) as data_port,
    ):
        eye = MxEye(
            MxEyeConfig(
                control_port=control_port, calibration_port=data_port, timeout=0.15
            )
        )
        try:
            yield eye, tracker
        finally:
            tracker.release.set()
            eye.close()


def config(unit=CalibrationCoordinateUnit.PIXELS):
    return CalibrationConfig(
        animal_info=AnimalInfo(animal_id="animal-1", metadata={"task": "calibration"}),
        screen_dimensions=ScreenDimensions(width=1920, height=1080),
        coordinate_unit=unit,
    )


def stimulus(timestamp=1_790_000_000_123_456_789):
    return CalibrationStimulus(
        stimulus_timestamp=timestamp,
        stimulus_position=CalibrationPosition(x=0.5, y=0.5),
        stimulus_size=CalibrationSize(width=0.05, height=0.1),
        stimulus_type="point",
    )


@pytest.mark.parametrize("unit", list(CalibrationCoordinateUnit))
def test_start_send_stop_and_separate_endpoints(service, unit):
    eye, tracker = service
    settings = config(unit)
    session = eye.start_calibration(settings)
    assert isinstance(session, CalibrationSession)
    assert session.calibration_id == settings.calibration_id
    assert session.status.last_sequence == 0
    # No connect() or acquisition start is required, and no frame is sent implicitly.
    assert tracker.data_requests == []
    for sequence in (1, 2):
        event = stimulus(1_790_000_000_123_456_789 + sequence)
        assert session.send(event).sequence == sequence
        assert (
            tracker.frames[sequence].stimulus.stimulus_timestamp
            == event.stimulus_timestamp
        )
    assert eye.status().calibration.last_sequence == 2
    assert session.stop().state is CalibrationState.STOPPED
    assert session.stop().last_sequence == 2
    assert session.finished
    assert [request.command for request in tracker.commands] == [
        Command.CALIBRATION_START,
        Command.STATUS,
        Command.CALIBRATION_STOP,
    ]
    assert tracker.commands[-1].calibration_stop.final_sequence == 2
    with pytest.raises(RuntimeError, match="stopped"):
        session.send(stimulus())


@pytest.mark.parametrize(
    "fault", ["timeout", "wrong_sequence", "wrong_id", "malformed", "multipart"]
)
def test_uncertain_send_retains_exact_frame_for_explicit_retry(service, fault):
    eye, tracker = service
    session = eye.start_calibration(config())
    event = stimulus()
    tracker.fault = fault
    with pytest.raises((TimeoutError, ValueError)):
        session.send(event)
    assert len(tracker.data_requests) == 1  # No automatic retry.
    assert session.status.last_sequence == 0
    with pytest.raises(RuntimeError, match="pending"):
        session.send(stimulus(event.stimulus_timestamp + 1))
    with pytest.raises(RuntimeError, match="pending"):
        session.stop()
    tracker.release.set()
    assert session.send(event).sequence == 1
    assert tracker.data_requests[0] == tracker.data_requests[1]
    assert len(tracker.frames) == 1
    assert session.stop().last_sequence == 1


def test_rejection_allows_corrected_event_without_advancing_sequence(service):
    eye, tracker = service
    session = eye.start_calibration(config())
    tracker.fault = "reject"
    with pytest.raises(RuntimeError, match="Invalid stimulus"):
        session.send(stimulus())
    assert session.status.last_sequence == 0
    assert session.send(stimulus(123)).sequence == 1


def test_stop_timeout_blocks_send_and_allows_explicit_stop_retry(service):
    eye, tracker = service
    session = eye.start_calibration(config())
    session.send(stimulus())
    tracker.hold_command = Command.CALIBRATION_STOP
    with pytest.raises(TimeoutError):
        session.stop()
    with pytest.raises(RuntimeError, match="stopping"):
        session.send(stimulus())
    tracker.release.set()
    assert session.stop().state is CalibrationState.STOPPED
    assert (
        sum(request.command is Command.CALIBRATION_STOP for request in tracker.commands)
        == 2
    )


def test_start_timeout_requires_same_config_and_retries_same_id(service):
    eye, tracker = service
    settings = config()
    tracker.hold_command = Command.CALIBRATION_START
    with pytest.raises(TimeoutError):
        eye.start_calibration(settings)
    with pytest.raises(RuntimeError, match="same config"):
        eye.start_calibration(config())
    tracker.release.set()
    assert eye.status().calibration.calibration_id == settings.calibration_id
    session = eye.start_calibration(settings)
    assert session.calibration_id == settings.calibration_id
    assert tracker.commands[0] == tracker.commands[-1]
    with pytest.raises(RuntimeError, match="already active"):
        eye.start_calibration(settings)


def test_start_rejection_allows_new_config(service):
    eye, tracker = service
    tracker.reject_command = Command.CALIBRATION_START
    with pytest.raises(RuntimeError, match="Rejected"):
        eye.start_calibration(config())
    assert eye.start_calibration(config()).status.state is CalibrationState.ACTIVE


def test_stop_rejection_allows_continuing(service):
    eye, tracker = service
    session = eye.start_calibration(config())
    tracker.reject_command = Command.CALIBRATION_STOP
    with pytest.raises(RuntimeError, match="Rejected"):
        session.stop()
    assert session.send(stimulus()).sequence == 1


def test_empty_session_and_new_session_after_stop(service):
    eye, tracker = service
    first = eye.start_calibration(config())
    assert first.stop().last_sequence == 0
    second = eye.start_calibration(config())
    assert first.calibration_id != second.calibration_id
    assert second.send(stimulus()).sequence == 1
    assert tracker.commands[1].calibration_stop.final_sequence == 0


@pytest.mark.parametrize("close_eye", [True, False])
def test_local_close_never_sends_remote_stop(service, close_eye):
    eye, tracker = service
    session = eye.start_calibration(config())
    eye.close() if close_eye else session.close()
    assert session.finished
    for operation in (lambda: session.send(stimulus()), session.stop):
        with pytest.raises(RuntimeError, match="closed"):
            operation()
    assert [request.command for request in tracker.commands] == [
        Command.CALIBRATION_START
    ]
    assert tracker.state is CalibrationState.ACTIVE


def test_session_rejects_other_threads_without_network_io(service):
    eye, tracker = service
    session = eye.start_calibration(config())
    with (
        concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor,
        pytest.raises(RuntimeError, match="creating thread"),
    ):
        executor.submit(session.send, stimulus()).result()
    assert tracker.data_requests == []


@pytest.mark.parametrize("fault", ["missing", "wrong_id", "wrong_state"])
def test_start_reply_requires_matching_calibration_state(service, fault):
    eye, tracker = service
    settings = config()

    def bad_reply(status):
        if fault == "missing":
            return status.model_copy(update={"calibration": None})
        changes = (
            {"calibration_id": uuid4()}
            if fault == "wrong_id"
            else {"state": CalibrationState.STOPPED}
        )
        return status.model_copy(
            update={"calibration": status.calibration.model_copy(update=changes)}
        )

    tracker.bad_control = bad_reply
    with pytest.raises(ValueError, match="requested calibration state"):
        eye.start_calibration(settings)
    assert eye.start_calibration(settings).calibration_id == settings.calibration_id


def test_stop_reply_requires_matching_final_sequence(service):
    eye, tracker = service
    session = eye.start_calibration(config())
    tracker.bad_control = lambda status: status.model_copy(
        update={
            "calibration": status.calibration.model_copy(update={"last_sequence": 1}),
        }
    )
    with pytest.raises(ValueError, match="final sequence"):
        session.stop()
    with pytest.raises(RuntimeError, match="stopping"):
        session.send(stimulus())
    assert session.stop().last_sequence == 0


def test_sdk_config_and_exports():
    settings = MxEyeConfig(calibration_port=6000)
    assert settings.calibration_port == 6000
    assert MxEyeConfig().calibration_port == 5558
    assert dataclasses.is_dataclass(settings)
    assert MxEyeConfig.__dataclass_params__.frozen


def test_receiver_rejects_gaps_conflicts_and_post_stop_events(service):
    eye, tracker = service
    session = eye.start_calibration(config())
    client = CalibrationClient(
        eye.config.host, eye.config.calibration_port, eye.config.timeout
    )
    frame = CalibrationDataFrame(
        calibration_id=session.calibration_id, sequence=1, stimulus=stimulus()
    )
    with pytest.raises(RuntimeError, match="gap"):
        client.send(frame.model_copy(update={"sequence": 2}))
    assert session.send(stimulus()).sequence == 1
    assert client.send(frame).sequence == 1
    with pytest.raises(RuntimeError, match="Conflicting"):
        client.send(frame.model_copy(update={"stimulus": stimulus(123)}))
    assert len(tracker.frames) == 1
    session.stop()
    with pytest.raises(RuntimeError, match="not active"):
        client.send(frame)


def test_stopped_id_is_not_restarted_as_a_new_session(service):
    eye, _tracker = service
    settings = config()
    eye.start_calibration(settings).stop()
    with pytest.raises(RuntimeError, match="already stopped"):
        eye.start_calibration(settings)
    assert eye.start_calibration(config()).status.state is CalibrationState.ACTIVE


def test_pending_start_snapshots_mutable_metadata(service):
    eye, tracker = service
    settings = config()
    tracker.hold_command = Command.CALIBRATION_START
    with pytest.raises(TimeoutError):
        eye.start_calibration(settings)
    settings.animal_info.metadata["task"] = "changed"
    with pytest.raises(RuntimeError, match="same config"):
        eye.start_calibration(settings)
    tracker.release.set()
    settings.animal_info.metadata["task"] = "calibration"
    assert eye.start_calibration(settings).calibration_id == settings.calibration_id
