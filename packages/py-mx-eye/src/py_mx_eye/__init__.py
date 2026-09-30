"""Python SDK for the mx-eye tracker: the public interface of the receiver side.

The SDK uses mx-eye-protocol and its Pydantic models; importing it never loads
Qt, OpenCV, or the tracker, and it owns no thread of its own.
"""

from mx_eye_protocol.calibration import (
    AnimalInfo,
    CalibrationAck,
    CalibrationConfig,
    CalibrationCoordinateUnit,
    CalibrationPosition,
    CalibrationSize,
    CalibrationState,
    CalibrationStatus,
    CalibrationStimulus,
    ScreenDimensions,
)

from .calibration import CalibrationSession
from .eye import MxEye, MxEyeConfig
from .sample import Sample

__version__ = "0.1.0"
__all__ = [
    "AnimalInfo",
    "CalibrationAck",
    "CalibrationConfig",
    "CalibrationCoordinateUnit",
    "CalibrationPosition",
    "CalibrationSession",
    "CalibrationSize",
    "CalibrationState",
    "CalibrationStatus",
    "CalibrationStimulus",
    "MxEye",
    "MxEyeConfig",
    "Sample",
    "ScreenDimensions",
]
