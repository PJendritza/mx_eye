"""Calibration wire contracts, including exact presentation timestamp retention."""

import json
from uuid import uuid4

import pytest
from mx_eye_protocol import (
    AnimalInfo,
    CalibrationAck,
    CalibrationConfig,
    CalibrationCoordinateUnit,
    CalibrationDataFrame,
    CalibrationPosition,
    CalibrationSize,
    CalibrationState,
    CalibrationStatus,
    CalibrationStimulus,
    CalibrationStop,
    Command,
    ControlReply,
    ControlRequest,
    ScreenDimensions,
    StatusSnapshot,
)
from pydantic import ValidationError


def config(unit=CalibrationCoordinateUnit.PIXELS):
    return CalibrationConfig(
        animal_info=AnimalInfo(
            animal_id="animal-1", metadata={"trial": 4, "tags": ["a"]}
        ),
        screen_dimensions=ScreenDimensions(width=1920, height=1080),
        coordinate_unit=unit,
    )


def stimulus():
    return CalibrationStimulus(
        stimulus_timestamp=1_790_000_000_123_456_789,
        stimulus_position=CalibrationPosition(x=123.5, y=400),
        stimulus_size=CalibrationSize(width=32, height=16),
        stimulus_type="image",
    )


@pytest.mark.parametrize("unit", list(CalibrationCoordinateUnit))
def test_start_and_status_round_trip(unit):
    settings = config(unit)
    request = ControlRequest(
        command=Command.CALIBRATION_START, protocol="1.1.0", calibration_start=settings
    )
    assert ControlRequest.model_validate_json(request.model_dump_json()) == request
    assert settings.screen_dimensions.width == 1920
    status = CalibrationStatus(
        calibration_id=settings.calibration_id, state=CalibrationState.ACTIVE
    )
    reply = ControlReply(status=StatusSnapshot(state="running", calibration=status))
    assert ControlReply.model_validate_json(reply.model_dump_json()) == reply


def test_event_round_trip_preserves_full_timestamp_and_off_screen_position():
    event = stimulus().model_copy(
        update={"stimulus_position": CalibrationPosition(x=-2, y=2)}
    )
    frame = CalibrationDataFrame(calibration_id=uuid4(), sequence=1, stimulus=event)
    assert CalibrationDataFrame.decode(frame.encode()) == frame
    assert (
        json.loads(frame.encode())["stimulus"]["stimulus_timestamp"]
        == 1_790_000_000_123_456_789
    )
    assert frame.stimulus.stimulus_position.x == -2
    ack = CalibrationAck(calibration_id=frame.calibration_id, sequence=frame.sequence)
    assert CalibrationAck.model_validate_json(ack.model_dump_json()) == ack


@pytest.mark.parametrize(
    "changes",
    [
        {"stimulus_timestamp": 0},
        {"stimulus_timestamp": 1.5},
        {"stimulus_timestamp": True},
        {"stimulus_position": {"x": float("nan"), "y": 0}},
        {"stimulus_position": {"x": 0, "y": float("inf")}},
        {"stimulus_size": {"width": 0, "height": 1}},
        {"stimulus_type": ""},
        {"extra": 1},
    ],
)
def test_invalid_stimuli(changes):
    with pytest.raises(ValidationError):
        CalibrationStimulus.model_validate(stimulus().model_dump() | changes)


@pytest.mark.parametrize(
    "dimensions", [{"width": 0, "height": 1}, {"width": 1.5, "height": 1}]
)
def test_invalid_dimensions(dimensions):
    with pytest.raises(ValidationError):
        ScreenDimensions.model_validate(dimensions)


@pytest.mark.parametrize("sequence", [0, -1, 1.5, True])
def test_invalid_sequences(sequence):
    with pytest.raises(ValidationError):
        CalibrationDataFrame(
            calibration_id=uuid4(), sequence=sequence, stimulus=stimulus()
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"ok": True, "error": "bad"},
        {"ok": False},
        {"ok": False, "error": ""},
        {"protocol": "2.0.0"},
    ],
)
def test_invalid_acks(changes):
    with pytest.raises(ValidationError):
        CalibrationAck(calibration_id=uuid4(), sequence=1, **changes)


def test_command_payloads_are_exclusive_and_versioned():
    settings = config()
    stop = CalibrationStop(calibration_id=settings.calibration_id, final_sequence=0)
    invalid = [
        {"command": Command.CALIBRATION_START},
        {
            "command": Command.CALIBRATION_START,
            "protocol": "1.0.0",
            "calibration_start": settings,
        },
        {"command": Command.CALIBRATION_STOP, "protocol": "1.1.0"},
        {"command": Command.STATUS, "calibration_start": settings},
        {
            "command": Command.CALIBRATION_START,
            "protocol": "1.1.0",
            "calibration_start": settings,
            "calibration_stop": stop,
        },
    ]
    for data in invalid:
        with pytest.raises(ValidationError):
            ControlRequest.model_validate(data)
    request = ControlRequest(
        command=Command.CALIBRATION_STOP, protocol="1.1.0", calibration_stop=stop
    )
    assert ControlRequest.model_validate_json(request.model_dump_json()) == request


def test_control_default_and_frozen_models():
    request = ControlRequest(command=Command.STATUS)
    assert json.loads(request.model_dump_json(exclude_none=True)) == {
        "command": "status",
        "protocol": "1.1.0",
    }
    assert StatusSnapshot(state="running").calibration is None
    with pytest.raises(ValidationError):
        stimulus().stimulus_timestamp = 2


def test_animal_identity_and_metadata():
    with pytest.raises(ValidationError):
        AnimalInfo(animal_id="")
    with pytest.raises(ValidationError):
        AnimalInfo(animal_id="a", metadata={"invalid": object()})
