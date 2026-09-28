"""Minimal SDK example: python -m mx_eye_client.receive_minimal.

Run it against a tracker that is already serving samples. The consumer is a
plain synchronous loop on the main thread: ``read()`` blocks that thread instead
of the SDK owning one.
"""

from contextlib import suppress

from py_mx_eye import MxEye, MxEyeConfig

TRACKER_IP = "127.0.0.1"

with suppress(KeyboardInterrupt), MxEye(MxEyeConfig(host=TRACKER_IP)) as eye:
    # read() yields one current measurement per iteration and waits on this
    # thread for the next one; pass a timeout to end the loop after silence.
    for sample in eye.read():
        payload = sample.frame.payload
        # Pixel signal: pupil minus CR, or pupil coordinates in pupil-only mode.
        print(f"x={payload.x:7.2f}  y={payload.y:7.2f}  age≈{sample.age_ms:5.2f} ms")
