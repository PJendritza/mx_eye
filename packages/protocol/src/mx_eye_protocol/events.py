"""Experiment context sent from MXBI Flow to the eye recorder."""

from enum import StrEnum
from typing import ClassVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class ExperimentEventKind(StrEnum):
    ANIMAL_ENTERED = "animal_entered"
    ANIMAL_IDENTIFIED = "animal_identified"
    ANIMAL_LEFT = "animal_left"
    DETECTOR_FAULT = "detector_fault"
    STIMULUS_SHOWN = "stimulus_shown"
    STIMULUS_CHANGED = "stimulus_changed"
    STIMULUS_HIDDEN = "stimulus_hidden"
    RUN_ENDED = "run_ended"


class ExperimentEvent(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False
    )

    event_id: UUID = Field(default_factory=uuid4)
    kind: ExperimentEventKind
    timestamp_ns: int = Field(ge=0)
    run_id: str = Field(min_length=1)
    visit_id: str | None = None
    rig_id: str = ""
    session_id: int | None = None
    animal: str | None = None
    rfid_id: str | None = None
    trial_id: int | None = None
    stage: str | None = None
    stimulus_id: str | None = None
    stimulus_type: str | None = None
    position: tuple[float, float] | None = None
    size: tuple[float, float] | None = None
    screen_dimensions: tuple[int, int] | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    # Explicit opt-in from the experiment; passive event logging is also supported.
    control_recording: bool = False
