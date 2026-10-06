# mx-eye-protocol

Shared typed tracking, control and calibration messages. This package has no
tracker, Qt, OpenCV or ZeroMQ dependency.

## Calibration transport

| Purpose | Pattern | Default port |
| --- | --- | ---: |
| Tracking samples | Tracker PUB bind, SDK SUB connect | 5556 |
| Commands, including calibration start/stop | Tracker REP bind, SDK REQ connect | 5557 |
| Calibration stimulus events and ACKs | Tracker REP bind, SDK REQ connect | 5558 |

Calibration events require individual acceptance. Each data request and ACK is
one UTF-8 JSON ZeroMQ frame, without topics, newline delimiters or binary headers.
The existing tracking binary layout is unchanged.

All commands use control protocol `1.1.0`; older control versions are rejected.
The separate calibration data frame and ACK protocol is `1.0.0`.

## Models and units

- `CalibrationConfig`: UUID `calibration_id`, `animal_info` (`animal_id` and
  optional JSON `metadata`), `screen_dimensions` and `coordinate_unit`.
- `CalibrationStimulus`: `stimulus_timestamp`, `stimulus_position` (center x/y),
  `stimulus_size` (bounding-box width/height) and nonempty `stimulus_type`.
- `CalibrationDataFrame`: composes a `CalibrationStimulus` in its `stimulus`
  field, alongside `calibration_id`, 1-based `sequence` and `protocol`;
  `encode()`/`decode()` preserve the timestamp exactly.
- `CalibrationAck`: `calibration_id`, `sequence`, `protocol`, `ok` and optional
  `error`. Acceptance is not a durability or calibration-completion guarantee.
- `CalibrationStatus`: ID, `active`/`stopped` state and `last_sequence` (initially
  0). Returned inside `StatusSnapshot.calibration`.
- `CalibrationStop`: ID and `final_sequence` (0 for an empty session).

`coordinate_unit` is `pixels` or `normalized`. Both use a top-left origin, x
rightward and y downward. In normalized units, screen width and height are each
1; positions and stimulus sizes use the same units. `screen_dimensions` always
contains the actual positive integer display resolution in pixels. Off-screen
positions are allowed and never clamped. All stimulus coordinates and sizes are
finite; sizes must be positive. Animal metadata and dimensions are sent once at
start, not repeated in every event.

### Time contract

Both machines' system clocks are assumed to be externally synchronized by
chrony. `stimulus_timestamp` is **the actual presentation time**, expressed as
integer Unix epoch nanoseconds from that synchronized system wall clock
(`CLOCK_REALTIME`, e.g. Python `time.time_ns()`). It shares the time base used by
tracking `acquisition_ns` and other wall-clock timestamps.

Neither package calls `chronyc`, checks synchronization, estimates an offset,
clamps timestamps, nor replaces the presentation time with the send time.
The protocol accepts a historical presentation timestamp. RPC deadlines use
the local monotonic clock and do not become presentation timestamps.

## Receiver contract for the future tracker implementation

The production tracker does not yet implement these commands or the calibration
data endpoint. Protocol and SDK tests use a test-only REP service.

1. Start accepts the configuration and returns a matching active calibration
   status. A retry of the identical configuration and ID returns the current
   status without resetting accepted events. Reject conflicting configurations
   and a different ID while another calibration is active. Start does not start
   eye acquisition.
2. Data messages must identify the active calibration. Accept sequences in
   order, starting at 1. A success ACK must echo the ID and sequence. A negative
   ACK echoes them too, and means the submitted event was not accepted.
3. Deduplicate identical ID/sequence/content retries and return acceptance again.
   Reject conflicting duplicates and gaps; do not replace already accepted data.
4. Stop must verify `final_sequence` against the accepted prefix, then return a
   matching stopped status with that last sequence. Repeating a matching stop is
   idempotent. Reject subsequent data for a stopped session and retain enough
   session history to recognize retries. Restarting a stopped ID must not
   accidentally create a fresh calibration.
5. A timeout does not cancel remote execution. Return `StatusSnapshot.calibration`
   on status queries so callers can inspect an uncertain start or stop.

Calibration fitting, persistence, raw-eye-data recording and gaze transformation
are outside this communication contract.

## Verification

From the workspace root, run `uv run pytest packages/protocol/tests`,
`uv run pyright`, and
`uv run pyright --verifytypes mx_eye_protocol --ignoreexternal`.
