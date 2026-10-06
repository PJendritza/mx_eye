# py-mx-eye

Synchronous SDK for tracking samples and calibration event submission. The SDK
creates no Python background thread and imports neither Qt nor OpenCV.

## Calibration

The tracker must implement the calibration protocol and bind its REP data
endpoint (default 5558); the current production tracker does not yet consume
these messages. Start/stop use the existing control endpoint (5557).

```python
import time

from py_mx_eye import (
    AnimalInfo,
    CalibrationConfig,
    CalibrationCoordinateUnit,
    CalibrationPosition,
    CalibrationSize,
    CalibrationStimulus,
    MxEye,
    MxEyeConfig,
    ScreenDimensions,
)

settings = CalibrationConfig(
    animal_info=AnimalInfo(animal_id="animal-1", metadata={"task": "nine-point"}),
    screen_dimensions=ScreenDimensions(width=1920, height=1080),
    coordinate_unit=CalibrationCoordinateUnit.PIXELS,
)
eye = MxEye(MxEyeConfig(host="192.168.50.2", calibration_port=5558))
try:
    session = eye.start_calibration(settings)

    # Present the stimulus and wait for its actual display flip here.
    # Capture this time at that flip, using the chrony-synchronized system clock.
    presentation_ns = time.time_ns()
    event = CalibrationStimulus(
        stimulus_timestamp=presentation_ns,
        stimulus_position=CalibrationPosition(x=960, y=540),
        stimulus_size=CalibrationSize(width=24, height=24),
        stimulus_type="point",
    )
    ack = session.send(event)
    result = session.stop()
finally:
    eye.close()
```

`start_calibration(settings)` returns a `CalibrationSession`; it does not open
the tracking SUB stream, start eye acquisition or send events automatically.
Use `eye.connect()`/`read()` and `eye.start()` independently when needed.
Only one calibration session may be active per `MxEye` handle. Use calibration
lifecycle calls serially and use each session on its creating thread.

The session owns the calibration ID and sequence. `send()` waits for an ACK and
advances the sequence only after acceptance. `session.status` contains the last
locally confirmed state, `session.calibration_id` exposes the ID and
`session.finished` indicates stopped or locally closed. `eye.status()` queries
the tracker and does not silently change the local session's retry state.

Both ends are assumed to have system clocks synchronized externally by chrony.
Provide the actual presentation time as Unix epoch nanoseconds, using the same
time base as eye tracking timestamps. The SDK trusts that prerequisite and
preserves the timestamp exactly; it does not run synchronization checks or
substitute send time. Hardware/display timestamps from another clock must first
be converted by the caller to this wall-clock time base.

For `NORMALIZED` coordinates, screen width and height each equal 1. The origin
is still top-left, x rightward and y downward. Positions and sizes use that unit;
`screen_dimensions` still contains the actual pixel resolution.

## Errors and explicit retries

Every RPC uses a fresh REQ socket and one monotonic deadline, bounded by
`MxEyeConfig.timeout`. There is no automatic resend.

- `TimeoutError`: remote execution may already have happened.
- `ValueError`: a malformed, multipart or mismatched response cannot certify
  acceptance and is treated as uncertain.
- `RuntimeError`: the tracker rejected the request, or the local session state
  does not permit the operation.

After an uncertain start, inspect `eye.status()` or explicitly retry
`eye.start_calibration(settings)` using the identical configuration and ID.
The SDK snapshots start metadata; changing it while start is uncertain is
rejected. A definitive rejection permits starting with another configuration.

After an uncertain send, explicitly call `session.send(event)` with identical
content, including the **original** presentation timestamp. It resends the same
ID/sequence/content. Another event and stop are blocked until this frame is
confirmed. A definitive negative ACK permits a corrected event at that sequence.

After an uncertain stop, explicitly call `session.stop()` again; sends remain
blocked. A confirmed stop can be called again without another network request.
An empty session stops with final sequence 0.

`session.close()` and `eye.close()` invalidate the local session without issuing
a remote stop. These operations do not cancel uncertain remote execution.
Call `session.stop()` explicitly when you need to finish remotely.

## Verification

From the workspace root, run `uv run pytest packages/py-mx-eye/tests`,
`uv run pyright`, and
`uv run pyright --verifytypes py_mx_eye --ignoreexternal`.
