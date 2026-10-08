"""Manual gaze calibration profiles and versioned local persistence."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .paths import calibration_root


class CalibrationProfile(BaseModel):
    """A reproducible mapping from tracker output pixels to screen pixels."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        frozen=True, extra="forbid", allow_inf_nan=False
    )

    format: Literal["mx-eye-calibration"] = "mx-eye-calibration"
    version: Literal["1.1.0"] = "1.1.0"
    calibration_id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    animal_id: str = Field(min_length=1)
    screen_width: int = Field(default=1024, gt=0, le=32768)
    screen_height: int = Field(default=600, gt=0, le=32768)
    input_coordinate_system: str = Field(default="pupil_minus_cr", min_length=1)
    offset_x: float = 0.0
    offset_y: float = 0.0
    gain_x: float = 1.0
    gain_y: float = 1.0
    rotation_deg: float = 0.0

    @model_validator(mode="before")
    @classmethod
    def migrate_origin_profiles(cls, value: Any) -> Any:
        """Convert the original origin-based transform to offset-only form."""
        if not isinstance(value, dict):
            return value
        data = dict(value)
        raw_x = float(data.pop("raw_origin_x", 0.0))
        raw_y = float(data.pop("raw_origin_y", 0.0))
        if raw_x or raw_y:
            gain_x = float(data.get("gain_x", 1.0))
            gain_y = float(data.get("gain_y", 1.0))
            theta = math.radians(float(data.get("rotation_deg", 0.0)))
            cosine, sine = math.cos(theta), math.sin(theta)
            origin_x = raw_x * gain_x
            origin_y = raw_y * gain_y
            data["offset_x"] = float(data.get("offset_x", 0.0)) - (
                cosine * origin_x - sine * origin_y
            )
            data["offset_y"] = float(data.get("offset_y", 0.0)) - (
                sine * origin_x + cosine * origin_y
            )
        data["version"] = "1.1.0"
        return data

    @model_validator(mode="after")
    def validate_gain(self) -> Self:
        if self.gain_x == 0 or self.gain_y == 0:
            raise ValueError("Calibration gains must be non-zero")
        return self

    @property
    def screen_center(self) -> tuple[float, float]:
        return self.screen_width / 2, self.screen_height / 2

    def apply(self, x: float, y: float) -> tuple[float, float]:
        """Transform one raw point into top-left-origin screen pixels."""
        if not (math.isfinite(x) and math.isfinite(y)):
            return math.nan, math.nan
        dx = x * self.gain_x
        dy = y * self.gain_y
        theta = math.radians(self.rotation_deg)
        cosine, sine = math.cos(theta), math.sin(theta)
        center_x, center_y = self.screen_center
        return (
            center_x + cosine * dx - sine * dy + self.offset_x,
            center_y + sine * dx + cosine * dy + self.offset_y,
        )

    def as_new_version(self) -> Self:
        return self.model_copy(
            update={"calibration_id": uuid4(), "created_at": datetime.now(UTC)}
        )


def default_calibration_root() -> Path:
    """Return the configured, visible mx_eye calibration directory."""
    return calibration_root()


def _legacy_calibration_root() -> Path:
    """Return the original AppData location for one-time migration."""
    if os.name == "nt":
        fallback = Path.home() / "AppData" / "Local"
        base = Path(os.environ.get("LOCALAPPDATA", fallback))
    else:
        fallback = Path.home() / ".local" / "share"
        base = Path(os.environ.get("XDG_DATA_HOME", fallback))
    return base / "mx_eye" / "calibrations"


class CalibrationStore:
    """Save immutable profiles and remember the active profile per animal."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or default_calibration_root()).expanduser()
        if (
            root is None
            and "MX_EYE_CALIBRATION_DIR" not in os.environ
            and not self.root.exists()
        ):
            legacy = _legacy_calibration_root()
            if legacy.exists() and legacy != self.root:
                shutil.copytree(legacy, self.root, dirs_exist_ok=True)
        self.index_path = self.root / "active.json"

    @staticmethod
    def _safe_animal_id(animal_id: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", animal_id.strip()).strip("._")
        if not safe:
            raise ValueError("Animal ID must contain a letter or number")
        return safe

    @staticmethod
    def _write_json(path: Path, data: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(data, indent=2, allow_nan=False), encoding="utf-8"
        )
        temporary.replace(path)

    def _read_index(self) -> dict[str, object]:
        if not self.index_path.exists():
            return {"version": 1, "last_animal": None, "active": {}}
        raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("active", {}), dict):
            raise TypeError("Invalid calibration index")
        return raw

    def save_and_activate(self, profile: CalibrationProfile) -> Path:
        saved = profile.as_new_version()
        animal = self._safe_animal_id(saved.animal_id)
        stamp = saved.created_at.astimezone(UTC).strftime("%Y-%m-%d_%H-%M-%S-%f")
        relative = Path(animal) / f"{stamp}.json"
        path = self.root / relative
        self._write_json(path, saved.model_dump(mode="json"))
        index = self._read_index()
        active_value = index.get("active", {})
        if not isinstance(active_value, dict):
            raise TypeError("Invalid calibration index")
        active = dict(active_value)
        active[saved.animal_id] = relative.as_posix()
        self._write_json(
            self.index_path,
            {"version": 1, "last_animal": saved.animal_id, "active": active},
        )
        return path

    def load_active(self, animal_id: str) -> CalibrationProfile | None:
        active = self._read_index().get("active", {})
        relative = active.get(animal_id) if isinstance(active, dict) else None
        if not isinstance(relative, str):
            return None
        path = (self.root / relative).resolve()
        root = self.root.resolve()
        if path.parent == root or root not in path.parents:
            raise ValueError("Calibration index points outside its data directory")
        return CalibrationProfile.model_validate_json(path.read_text(encoding="utf-8"))

    def load_last_active(self) -> CalibrationProfile | None:
        animal = self._read_index().get("last_animal")
        return self.load_active(animal) if isinstance(animal, str) else None
