"""Calibration session metadata and acknowledged stimulus-event messages.

Presentation timestamps use chrony-synchronized CLOCK_REALTIME, in Unix epoch
nanoseconds, just like tracking timestamps. Synchronization is an external
precondition; neither the protocol nor the SDK estimates clock offsets.
"""

from enum import StrEnum, auto
from typing import ClassVar, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class CalibrationCoordinateUnit(StrEnum):
    PIXELS = auto()
    NORMALIZED = auto()


class CalibrationState(StrEnum):
    ACTIVE = auto()
    STOPPED = auto()


class CalibrationPosition(BaseModel):
    """Stimulus center: top-left origin, positive x rightward and y downward."""

    model_config: ClassVar[ConfigDict] = _MODEL_CONFIG

    x: float
    y: float


class CalibrationSize(BaseModel):
    """Stimulus bounding-box size, in the session's coordinate unit."""

    model_config: ClassVar[ConfigDict] = _MODEL_CONFIG

    width: float = Field(gt=0)
    height: float = Field(gt=0)


class ScreenDimensions(BaseModel):
    """Physical display resolution in pixels, also for normalized sessions."""

    model_config: ClassVar[ConfigDict] = _MODEL_CONFIG

    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)


class AnimalInfo(BaseModel):
    model_config: ClassVar[ConfigDict] = _MODEL_CONFIG

    animal_id: str = Field(min_length=1)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class CalibrationConfig(BaseModel):
    """Sent once at start. Reuse this object and ID when explicitly retrying.

    Normalized screen width and height are each 1; stimulus positions and sizes
    use those units. Pixel sessions use screen pixels. Off-screen positions are
    allowed in either unit and are never clamped.
    """

    model_config: ClassVar[ConfigDict] = _MODEL_CONFIG

    calibration_id: UUID = Field(default_factory=uuid4)
    animal_info: AnimalInfo
    screen_dimensions: ScreenDimensions
    coordinate_unit: CalibrationCoordinateUnit = CalibrationCoordinateUnit.PIXELS


class CalibrationStimulus(BaseModel):
    """One presented stimulus, with its original presentation timestamp.

    The caller captures stimulus_timestamp from the externally synchronized
    system wall clock at presentation (e.g. time.time_ns()), not at send time.
    """

    model_config: ClassVar[ConfigDict] = _MODEL_CONFIG

    stimulus_timestamp: int = Field(gt=0, strict=True)
    stimulus_position: CalibrationPosition
    stimulus_size: CalibrationSize
    stimulus_type: str = Field(min_length=1)


class CalibrationDataFrame(BaseModel):
    """One JSON message on the dedicated calibration REQ/REP endpoint."""

    model_config: ClassVar[ConfigDict] = _MODEL_CONFIG

    protocol: Literal["1.0.0"] = "1.0.0"
    calibration_id: UUID
    sequence: int = Field(gt=0, strict=True)
    stimulus: CalibrationStimulus

    def encode(self) -> bytes:
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def decode(cls, data: bytes) -> "CalibrationDataFrame":
        return cls.model_validate_json(data)


class CalibrationAck(BaseModel):
    """Acceptance, not a promise of persistence or completed calibration."""

    model_config: ClassVar[ConfigDict] = _MODEL_CONFIG

    protocol: Literal["1.0.0"] = "1.0.0"
    calibration_id: UUID
    sequence: int = Field(gt=0, strict=True)
    ok: bool = True
    error: str | None = None

    @model_validator(mode="after")
    def validate_result(self) -> "CalibrationAck":
        if (self.ok and self.error is not None) or (not self.ok and not self.error):
            raise ValueError("An ACK must contain acceptance or an error")
        return self


class CalibrationStatus(BaseModel):
    model_config: ClassVar[ConfigDict] = _MODEL_CONFIG

    calibration_id: UUID
    state: CalibrationState
    last_sequence: int = Field(default=0, ge=0, strict=True)


class CalibrationStop(BaseModel):
    """Stop only after all frames through final_sequence have been accepted."""

    model_config: ClassVar[ConfigDict] = _MODEL_CONFIG

    calibration_id: UUID
    final_sequence: int = Field(ge=0, strict=True)
