"""Python SDK for the mx-eye tracker: the public interface of the receiver side.

The SDK uses mx-eye-protocol and its Pydantic JSON models; importing it never
loads Qt, OpenCV, or the tracker.
"""

from .py_mx_eye import Client, Sample

__version__ = "0.1.0"
__all__ = ["Client", "Sample"]
