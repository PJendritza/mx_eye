# pyright: strict
"""Typed, versioned settings. All image coordinates use source pixels."""

import json
from enum import StrEnum, auto
from ipaddress import IPv4Address
from pathlib import Path
from typing import Literal, Self

from mx_eye_protocol.control import SourceMode
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)


class ConfigFormat(StrEnum):
    MX_EYE = "mx-eye"


class CameraBackend(StrEnum):
    AUTO = auto()
    DSHOW = auto()
    MSMF = auto()
    V4L2 = auto()


class TrackingMode(StrEnum):
    PUPIL_CR = "Pupil + CR"
    PUPIL_ONLY = "Pupil only"


class PupilCoordinates(StrEnum):
    ABSOLUTE = auto()
    RELATIVE = auto()


class RecordingCodec(StrEnum):
    MJPG = "MJPG"
    FFV1 = "FFV1"


class PupilMethod(StrEnum):
    THRESHOLD = "threshold"
    STARBURST = "starburst"
    EDGE_ELLIPSE = "edge_ellipse"
    ADAPTIVE = "adaptive"


PUPIL_METHODS = {
    PupilMethod.THRESHOLD: "Threshold (original)",
    PupilMethod.STARBURST: "Starburst-style",
    PupilMethod.EDGE_ELLIPSE: "Edge + ellipse",
    PupilMethod.ADAPTIVE: "Adaptive threshold",
}


class ConfigModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", allow_inf_nan=False, validate_assignment=True
    )


class SourceConfig(ConfigModel):
    mode: SourceMode = SourceMode.CAMERA
    camera: int = Field(default=0, ge=0)
    camera_name: str = ""
    device: str = ""
    exposure_mode: Literal["unchanged", "manual", "auto"] = "unchanged"
    exposure_ms: float = Field(default=7.8125, ge=0.01, le=1000)
    gain: int | None = Field(default=None, ge=0, le=65535)
    path: str = ""
    width: int = Field(default=640, ge=32, le=16384)
    height: int = Field(default=480, ge=32, le=16384)
    fps: float = Field(default=120.0, ge=1, le=1000)
    backend: CameraBackend = CameraBackend.AUTO
    fourcc: str = Field(default="MJPG", min_length=4, max_length=4)
    speed: float = Field(default=1.0, ge=0.05, le=8)


class TrackingParameters(ConfigModel):
    pupil_method: PupilMethod = PupilMethod.THRESHOLD
    pupil_rays: int = Field(default=48, ge=16, le=128)
    pupil_edge_contrast: int = Field(default=8, ge=1, le=100)
    pupil_edge_threshold: int = Field(default=30, ge=1, le=255)
    pupil_fit_error: float = Field(default=2.5, ge=0.5, le=10)
    pupil_adaptive_window: int = Field(default=61, ge=3, le=301)
    pupil_adaptive_offset: int = Field(default=7, ge=0, le=50)
    pupil_thr: int = Field(default=48, ge=0, le=255)
    pupil_min: int = Field(default=30, ge=0)
    pupil_max: int = Field(default=3000, ge=0)
    cr_thr: int = Field(default=181, ge=0, le=255)
    cr_min: int = Field(default=2, ge=0)
    cr_max: int = Field(default=120, ge=0)
    pupil_gate: float = Field(default=7.0, gt=0)
    cr_gate: float = Field(default=2.0, gt=0)
    max_pair_dist: float = Field(default=10.0, gt=0)
    max_pair_vec_change: float = Field(default=4.0, gt=0)
    reacquire_after_frames: int = Field(default=4, gt=0)
    template_radius: int = Field(default=35, gt=0)
    template_search_size: int = Field(default=520, gt=0)
    template_min_corr: float = Field(default=0.60, ge=0, le=1)
    tracking_mode: TrackingMode = TrackingMode.PUPIL_CR
    pupil_coordinates: PupilCoordinates = PupilCoordinates.ABSOLUTE
    template_tracking: bool = False

    @model_validator(mode="after")
    def validate_areas(self) -> "TrackingParameters":
        if self.pupil_min > self.pupil_max or self.cr_min > self.cr_max:
            raise ValueError("Minimum blob area must not exceed maximum.")
        return self


class TrackingConfig(TrackingParameters):
    roi: tuple[float, float, float, float] = (0.0, 0.0, 320.0, 240.0)

    @model_validator(mode="after")
    def validate_roi(self) -> "TrackingConfig":
        if self.roi[2] < 1 or self.roi[3] < 1:
            raise ValueError("ROI width and height must be at least one pixel.")
        return self


class NetworkConfig(ConfigModel):
    bind: IPv4Address = IPv4Address("127.0.0.1")
    data_port: int = Field(default=5556, ge=1024, le=65535)
    control_port: int = Field(default=5557, ge=1024, le=65535)

    @model_validator(mode="after")
    def validate_distinct_ports(self) -> "NetworkConfig":
        ports = {self.data_port, self.control_port}
        if len(ports) != 2:
            raise ValueError("Use two distinct ports between 1024 and 65535.")
        return self


class RecordingConfig(ConfigModel):
    directory: str = "recordings"
    buffer_mb: int = Field(default=128, ge=8, le=2048)
    codec: RecordingCodec = RecordingCodec.MJPG
    record_simulation: bool = False
    camera_mjpeg_passthrough: bool = True


class DisplayConfig(ConfigModel):
    hz: float = Field(default=25.0, gt=0)
    masks: bool = True
    crosshairs: bool = True
    pupil_mask: bool | None = None
    cr_mask: bool | None = None
    template_circle: bool | None = None
    template_inset: bool | None = None
    rejection_reason: bool = True
    suspended: bool = False


class TemplateConfig(ConfigModel):
    png: str
    anchor: tuple[float, float]
    center: tuple[float, float]


class MxEyeConfigModel(ConfigModel):
    format: ConfigFormat = ConfigFormat.MX_EYE
    version: Literal["1.0.0"] = "1.0.0"
    source: SourceConfig = Field(default_factory=SourceConfig)
    tracking: TrackingConfig = Field(default_factory=TrackingConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    recording: RecordingConfig = Field(default_factory=RecordingConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    template: TemplateConfig | None = None


class MxEyeConfigStore:
    def __init__(self, value: MxEyeConfigModel | None = None) -> None:
        self._config = value or MxEyeConfigModel()

    @property
    def value(self) -> MxEyeConfigModel:
        return self._config

    def replace(self, value: MxEyeConfigModel) -> None:
        """Adopt another model so a shared store keeps its identity."""
        self._config = value.model_copy(deep=True)

    @classmethod
    def defaults(cls) -> Self:
        return cls()

    @staticmethod
    def _check_path(path: object) -> Path:
        if not isinstance(path, Path):
            raise TypeError("path must be a pathlib.Path")
        return path

    @classmethod
    def load(cls, path: Path) -> Self:
        path = cls._check_path(path)
        raw: object = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("format") == "mx-eye" and raw.get("version") == 1:
            # Import settings from the original desktop app. The removed clock
            # sync endpoint has no equivalent in the new protocol.
            raw = dict(raw)
            raw["version"] = "1.0.0"
            network = raw.get("network")
            if isinstance(network, dict):
                raw["network"] = {key: value for key, value in network.items()
                                  if key != "sync_port"}
        return cls(MxEyeConfigModel.model_validate(raw))

    def save(self, path: Path) -> None:
        path = self._check_path(path)
        data = self.value.model_dump(mode="json", exclude_none=True)
        template = self.value.template
        data.update(template=template.model_dump(mode="json") if template else None)

        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(
            json.dumps(data, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        temp.replace(path)


_shared: MxEyeConfigStore | None = None


def store() -> MxEyeConfigStore:
    """The process-wide store shared by the GUI and the service.

    Worker processes receive a per-session copy instead; a module-level
    singleton cannot cross a process boundary.
    """
    global _shared
    if _shared is None:
        _shared = MxEyeConfigStore()
    return _shared


def configure(path: Path | None = None) -> MxEyeConfigStore:
    """Load a configuration file, or the defaults, into the shared store."""
    value = MxEyeConfigStore.load(path).value if path else MxEyeConfigModel()
    store().replace(value)
    return store()
