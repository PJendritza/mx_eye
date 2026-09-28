"""Python SDK for the mx-eye tracker: the public interface of the receiver side.

The SDK uses mx-eye-protocol and its Pydantic models; importing it never loads
Qt, OpenCV, or the tracker, and it owns no thread of its own.
"""

from .eye import MxEye, MxEyeConfig
from .sample import Sample

__version__ = "0.1.0"
__all__ = ["MxEye", "MxEyeConfig", "Sample"]
