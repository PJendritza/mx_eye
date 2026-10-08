"""Stable, visible locations for mx_eye runtime data."""

from __future__ import annotations

import os
from pathlib import Path

_data_root: Path | None = None


def set_data_root(path: Path | None) -> None:
    """Override the runtime-data directory for this process."""
    global _data_root
    _data_root = path.expanduser().resolve() if path is not None else None


def data_root() -> Path:
    """Return the configured runtime-data directory.

    Source checkouts default to a visible ``mx_eye_data`` sibling. Installed
    launches use the same folder in the user's home directory.
    """
    if _data_root is not None:
        return _data_root
    override = os.environ.get("MX_EYE_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    working = Path.cwd().resolve()
    if working.name.casefold() == "mx_eye" and (working / "pyproject.toml").exists():
        return working.parent / "mx_eye_data"
    return Path.home() / "mx_eye_data"


def config_path() -> Path:
    return data_root() / "config.json"


def calibration_root() -> Path:
    override = os.environ.get("MX_EYE_CALIBRATION_DIR")
    return (
        Path(override).expanduser().resolve()
        if override
        else data_root() / "calibrations"
    )
