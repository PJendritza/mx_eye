"""Typed JSON requests and responses for the single command REQ/REP endpoint."""

from enum import StrEnum, auto
from ipaddress import IPv4Address
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Command(StrEnum):
    STATUS = auto()
    START = auto()
    STOP = auto()


class SourceMode(StrEnum):
    CAMERA = auto()
    VIDEO = auto()
    SIMULATION = auto()


class ControlModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False
    )


class TrackingStats(ControlModel):
    """Counters and measurements reported by the tracking service."""

    acquired: int = 0
    tracked: int = 0
    written: int = 0
    enqueued: int = 0
    tracking_skips: int = 0
    mailbox_misses: int = 0
    record_fault: int = 0
    log_fault: int = 0
    capture_fault: int = 0
    tracking_fault: int = 0
    send_errors: int = 0
    source_fps: float = 0.0
    source_index: int = 0  # Zero-based media frame index.
    processing_us: float = 0.0  # Most recent tracking duration, microseconds.


class NetworkStatus(ControlModel):
    bind: IPv4Address = IPv4Address("127.0.0.1")
    data_port: int = 5556
    control_port: int = 5557


class SourceStatus(ControlModel):
    mode: SourceMode | None = None
    fps: float = 0.0
    total: int = 0  # Estimated video frame count until total_exact becomes true.
    total_exact: bool = False
    width: int = 0
    height: int = 0
    actual_format: str = "unknown format"
    driver_fps: float | None = None
    requested_format: str = ""
    requested_fps: float = 0.0


class StatusSnapshot(ControlModel):
    state: str
    message: str = ""
    paused: bool = False
    session: int | None = None
    stats: TrackingStats = Field(default_factory=TrackingStats)
    directory: str = ""
    network: NetworkStatus = Field(default_factory=NetworkStatus)
    source: SourceStatus = Field(default_factory=SourceStatus)
    priority: list[str] = Field(default_factory=list)
    server_errors: list[str] = Field(default_factory=list)


class ControlRequest(ControlModel):
    """One command sent by a REQ socket to the control service."""

    command: Command
    protocol: Literal["1.0.0"] = "1.0.0"  # Supported control protocol SemVer.


class ControlReply(ControlModel):
    """One response: a status snapshot or an error."""

    ok: bool = True
    status: StatusSnapshot | None = None
    error: str | None = None

    @model_validator(mode="after")
    def validate_result(self) -> "ControlReply":
        if self.ok:
            if self.status is None or self.error is not None:
                raise ValueError("A successful reply must contain exactly one result")
        elif self.status is not None or not self.error:
            raise ValueError("A failed reply must contain only an error message")
        return self
