"""Minimal SDK example: python -m mx_eye_client.receive_minimal.

Run it against a tracker that is already serving samples.
"""

import time

from py_mx_eye import Client

TRACKER_IP = "127.0.0.1"

with Client(TRACKER_IP) as eye:
    eye.start()
    try:
        while True:
            sample = eye.latest(max_age_ms=50)
            if sample is not None:
                # Pixel signal: pupil minus CR, or pupil coordinates in pupil-only mode.
                print(
                    f"x={sample.frame.payload.x:7.2f}  y={sample.frame.payload.y:7.2f}  age≈{sample.age_ms:5.2f} ms"
                )
            time.sleep(0.02)
    except KeyboardInterrupt:
        eye.stop()
