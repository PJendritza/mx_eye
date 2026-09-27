# mx_eye implementation details

Implementation details that used to live in the README: tracker controls, process
and buffering constraints, recording integrity, the wire protocol, delay readouts
and provenance. The workspace members live in `packages/mx-eye`,
`packages/mx-eye-client`, `packages/protocol` and `packages/py-mx-eye`.

## Tracker controls

- **Frame navigation fix:** seeking pauses on the selected frame. Frame numbers
  and slider positions come from the displayed preview. Held arrow keys request
  the next step only after the previous step is displayed, preventing a backlog.
  Template/feature selection first freezes the displayed video frame. Recent
  decoded frames are cached (64 MiB); larger backward jumps decode from the start
  to preserve exact frame numbering, so long jumps can take time. The timeline
  initially uses the container's frame-count estimate and corrects it when the
  decoder reaches the end. End-of-file leaves the last frame available to step back.
  A focused check with a generated H.264/MKV clip verified frame identities after
  seeks and steps, EOF correction, and template selection without changing frames.
  Windows GUI keyboard/mouse behavior still needs checking on the target machine.

- **Pupil-only output:** choose Absolute (full-image top-left origin) or Relative
  (yellow ROI top-left origin) above the X/Y plots, beneath the video. Applies to displayed,
  transmitted and recorded X/Y. Pupil + CR remains pupil minus CR. A moving ROI
  changes the relative origin; this is not calibrated gaze or full head-motion
  compensation. Raw pupil_x/pupil_y remain absolute. The SDK exposes
  `sample.frame.payload.coordinate_system`; packet/CSV flag bit 32 marks ROI-relative output.
- **Video navigation:** Left/Right arrows pause and step one frame backward/forward.
  Parameter editors keep their normal arrow-key behavior. Click anywhere on the
  timeline to seek immediately, or drag and release. Play/Pause changes its label
  with playback state. At the last frame playback pauses; Play restarts at frame 1.
- Hover over controls and parameters for short explanations and units.

- **Camera settings → Camera:** on Windows, connected DirectShow cameras are listed
  by name. Refresh after plugging in a camera. Selection prefers the highest
  resolution with a reported mode of at least 29 fps, or the fastest reported
  mode if none qualifies. Choose a resolution and camera format (for example,
  MJPG or YUY2) separately; the requested FPS starts at that format's reported
  maximum. The source line shows the format and FPS reported by the opened camera,
  while ACQ shows the measured rate. Discovery runs in a
  separate process and is available while tracking is stopped. If enumeration is
  unavailable, manual settings remain available. Other operating systems currently
  use manual camera settings. The Windows dependency comes from the `gui` extra
  installed by `uv sync --all-extras`.
- **Area limit preview:** adjusting pupil or CR minimum/maximum area shows a blue
  or red disk at the bottom-left of the eye-detail image for three seconds after
  the last adjustment. Its radius is `sqrt(area / pi)` in source pixels, scaled
  with the displayed ROI. An 80%-opaque black square keeps it readable. Oversized
  disks are clipped and marked, never silently shrunk. These are display overlays
  and are not included in the recorded video.

- **Camera / Video / Simulation:** choose the source. Stop before switching sources.
- **Open video:** choose an existing file; it begins playback immediately. Pause, advance one frame,
  or seek with the bottom timeline. While paused, threshold/ROI edits update the
  image without emitting a new sample or advancing tracking history.
- **Full view:** drag to move the yellow ROI; drag its lower-right corner to resize;
  Shift-drag to draw a new ROI. The mouse wheel zooms the source view around the
  pointer. Above 100% zoom, a zoom label and Reset button appear inside the view.
- **Eye detail:** left click seeds the pupil and estimates its threshold; right click
  seeds the corneal reflection. Thresholds remain manually adjustable.
- **Template:** right click in the full view, or Shift-click in the eye detail view.
  The template moves the eye ROI while its correlation passes the threshold.
  Drag the magenta search-box border to move its fixed search region. Change its
  size with the Template / ROI slider. Re-picking preserves that search region.
- **Tracking mode:** choose Pupil + CR (pupil minus CR) or Pupil only (absolute or ROI-relative pupil position).
- Click section headings to collapse/expand sliders. Numeric boxes accept typed
  values as well as small increments. The controls scroll when space is tight.
- **Settings:** camera index, requested mode/FPS, network addresses/ports, recording
  location, buffer size, and codec. Changes require a stopped session.
- **Visible controls:** Load/Save config in the top row; camera index and requested FPS beside
  them. Playback speed is next to Pause and the timeline, and can change live.
- **Video layout:** equally sized eye-detail (left) and source (right) panels.
  Pupil/CR mask switches and Centers sit above eye detail. Template size and Template inset above
  the source independently toggle the template-radius circle and small template
  preview. Click instructions stay below each video. Slider values sit beside
  their sliders in compact, collapsible groups.
- **Configuration:** Save/Load JSON also preserves these display switches.
  Configurations include the template image. The file is a `format: "mx-eye"`,
  `version: "1.0.0"` document validated by the typed pydantic models in
  `packages/mx-eye/src/mx_eye/config/`: unknown keys, wrong types and
  out-of-range values are refused on load, on save and on live assignment, while
  absent groups and keys fall back to the defaults. Saving writes the file
  atomically and omits unset display switches. The GUI resolves the single
  process-wide store from `config.store()` and injects it into the service, while
  every worker process receives a per-session copy.
- **Load/Save template:** visible top-row buttons import an image or export PNG.

Camera index 0 is usually the first camera. On Windows, Auto chooses DirectShow;
MSMF is available if the camera works better with that backend. Set a camera mode
supported by your OV9281, for example 640x480, requested 120 FPS, MJPG. These are
requests, not promises from the camera. The acquisition-rate readout is measured.
OpenCV receives UVC cameras; Raspberry Pi CSI/libcamera capture is not implemented.

## What runs independently

1. **Acquisition process:** reads the source and timestamps each returned frame.
   Publishes the latest frame to tracking before offering it to recording.
2. **Tracking process:** v13 pupil/CR and template logic, then nonblocking sample
   transmission. It does no GUI rendering, video encoding, or disk writing.
3. **Recording process:** consumes a separate bounded FIFO, encodes full frames,
   and writes frame timestamps and tracking rows.
4. **GUI:** displays the latest preview frame and updates plots at 25 Hz. It never
   requests or schedules the next camera frame.

The source-to-tracker mailbox, UI preview mailbox, and recording FIFO use shared memory. Slot
ownership protects frame bytes from being overwritten during a read. Recording
can retain every acquired frame even when tracking deliberately skips older frames.
The SKIPPED counter shows acquired frames not processed by the tracker.
File playback waits for each frame to be processed; heavy tracking may therefore
slow playback, rather than skipping source frames.

Acquisition/tracking request Above Normal priority on Windows and nice -5 on
Linux. Recording requests a lower CPU priority. If the OS denies a priority
change, the worker continues at normal priority and reports that in SDK status.
OpenCV uses one worker thread per process to reduce oversubscription.
This is best-effort scheduling, **not a hard real-time guarantee**. CPU, memory
bandwidth, camera/USB buffering, and OS scheduling still affect latency.

Run the tracker through `uv run mx-eye` or `python -m mx_eye`; both are safe with
multiprocessing. Running individual GUI/worker definitions interactively is not supported.

## Recording and integrity

Every **camera session records automatically**, from Start until Stop. Simulation
recording is optional; file playback does not duplicate the source video.
Each session gets a unique subfolder under the configured output directory:

| File | Contents |
|---|---|
| `video.avi` | MJPG full-frame video; fast, lossy compression, no overlays |
| `video.mkv` | Alternative FFV1 lossless full-frame video; more CPU demand |
| `frames.csv` | Video index, acquired source-frame ID, host acquisition timestamp, media time |
| `tracking.csv` | Every locally logged tracking sample, with timestamps, coordinates and flags |
| `config.json` | Configuration at session start |
| `session.json` | Final counts, completion status and any detected fault |

`frames.csv` is the timing authority. Video containers use a nominal constant FPS;
actual camera frame intervals may vary. Frame IDs connect video to tracking rows.
Parameter adjustments during acquisition affect tracking.csv; config.json records
starting settings only. Save the final configuration separately if needed.

The configurable memory budget is a finite buffer, not an unlimited guarantee.
If the writer cannot keep up and that buffer fills, recording stops accepting new
frames, the queued prefix is finalized, and the session is marked incomplete.
**Tracking continues.** A disk/write fault is similarly visible. Restart a new
session after resolving the fault. It is impossible to guarantee both unlimited
lossless recording and nonblocking tracking on a stalled disk with finite RAM.

Stop initiates orderly shutdown and buffer draining. Wait for IDLE or ERROR before
starting another session or removing the disk. Closing the tracker waits for this.
A hung camera is forcibly stopped after 5 seconds; hung tracking/writing after
30 seconds. Forced termination marks the session incomplete. After abrupt power
loss, a missing session.json must also be treated as an incomplete session.

Completion checks compare acquired/enqueued/written counts and reopen the video
container to check readability and reported frame count. This is not a full
post-recording decode of every frame. The backend cannot expose frames lost inside
the camera or USB driver. `tracking_log_complete` separately reports log overflow.

## SDK details

`latest()` can initially return None while clock synchronization or tracking is
starting. It also rejects invalid/lost or stale samples; never use the previous
valid point as if it were a current measurement. `latest(max_age_ms=None,
require_valid=False)` is intended for diagnostics. `drain()` returns buffered
samples for plotting/logging and clears that buffer. A background thread receives
regardless of how often your code calls either method. Its bounded buffer reports
overwrites rather than blocking. Use `status()` to inspect tracker/recording state.
Closing a client alone does **not** stop the tracker; `stop()` is explicit.

Coordinates are **uncalibrated source-image pixels**, positive x rightward and y
downward. They are not screen coordinates or visual degrees. Invalid signals use
NaN and lack the VALID flag. Feature loss is not labelled a blink automatically.
The template correlation is NCC, not a calibrated pupil-confidence probability.
No gaze calibration or neural eye-region detector is included in this first version.

The SDK is the `py-mx-eye` workspace member; it is not published to PyPI yet, so use it
from a checkout (`uv sync --all-extras`) or install the wheels built by `uv build` for
`py-mx-eye` and `mx-eye-protocol`. It uses `mx-eye-protocol` and its Pydantic JSON models, and never loads Qt or OpenCV.

## Networking

Defaults are local-machine only:

| Purpose | Transport | Port |
|---|---|---:|
| Tracking samples | TCP, optionally UDP | 5556 |
| Start/stop/status and clock synchronization | TCP | 5557 |

For separate computers: set tracker bind address to `0.0.0.0`, use its LAN IP in
`Client(...)` or `uv run mx-eye-receiver --host TRACKER_IP`, and allow the
configured ports through the local firewall. With UDP, also set the receiver's
LAN IP in tracker Settings. UDP has one target; TCP accepts up to eight receivers.
The SDK reads the configured transport from the status server when connecting.
Both address fields are validated as IPv4 literals, so hostnames are rejected
instead of being resolved at run time.
Reconnect clients after changing transport/ports. The protocol is unauthenticated
and intended for a trusted lab network, not an exposed internet service.

Sample TCP uses length-prefixed binary framing, disables Nagle, and never waits for slow
receivers: a partial/blocked send disconnects that receiver, which reconnects.
UDP can lose/reorder datagrams. Both include a session ID, sample sequence, and
source-frame ID. The SDK rejects out-of-order samples, resets on a new session,
and reports sequence gaps. Neither mode guarantees delivery of every sample.

The sample frame header is little-endian `<4sBI`: magic `MXEY`, message type
(`DATA=1`, `CMD=2`), and uint32 encoded payload length (excluding the header).
DATA carries a 97-byte `<7Qq8fB` TrackingPayload; the full frame is 106 bytes.
It replaces the old 104-byte v1 protocol and is not compatible with the earlier
`eye_sender_switchable_v4.py` experiment. Update tracker and SDK together.
TCP validates the header before buffering the declared payload and disconnects
on malformed headers; UDP drops malformed datagrams. Only DATA is implemented;
The binary CMD type is reserved. All commands, including sync, use newline-delimited
JSON on the single control port. The TCP server handles concurrent connections,
each with one request and one response followed by connection closure.
Request, Reply and nested status fields are Pydantic models. Command, Transport
and SourceMode use StrEnum with auto(); configuration reuses the same enums.
The control protocol version is the SemVer string `1.0.0`; other versions,
including legacy integers, are rejected. Packages require Python 3.11 or newer.
Replies contain either a nested status, sync timestamps, or an error.
SDK start/stop/status return StatusSnapshot with field access such as
`status.network.transport` and `status.stats.tracked`.
Remove legacy `sync_port` configuration/client arguments and `--sync-port`;
old flat status responses are no longer supported. Update both endpoints together.

`packages/protocol/src/mx_eye_protocol/data_frame.py` defines TrackingPayload,
DataFrame and flags. DataFrame contains magic, message_type, length and payload;
DataFrame owns the shared binary layout and exposes `from_payload()`, `encode()`
and the `frame_size` property. Encoding does not validate the header.
`DataFrame.header_size` is the stream header length. The SDK implements decoding
and incoming-header validation in `py_mx_eye/_decoder.py`.
The SDK Sample contains the complete frame plus reception and clock synchronization
information. Use `sample.frame.payload` to access tracking fields.
Recording queues carry TrackingPayload objects; the recording layer maps them to
the unchanged CSV columns without binary float32 conversion.

TrackingPayload flags use TrackingFlags (IntFlag); combine members with `|`.
`TrackingFlags.NONE` means no flags. The former Packet type and flat Sample
sampling attributes have been removed. Use `sample.frame.length` for encoded
payload size and `sample.age_ms` for reception timing.
TCP reception handles split and coalesced frames, rejecting unknown message
types, reserved CMD frames and invalid DATA lengths before buffering their bodies.
UDP uses the same binary envelope. Control JSON does not use that envelope.
Enum values remain lowercase strings in JSON. Successful replies contain
`ok: true` and either `status` or `sync`; failures contain `ok: false` and `error`.
For example, a status request is `{"command":"status","protocol":"1.0.0"}`
followed by a newline.

## Type checking

`mx-eye-protocol` ships inline type annotations and a PEP 561 `py.typed` marker
in both wheels and source distributions. After installing workspace development
dependencies, run `uv run pyright` from the repository root. Strict checking covers
`packages/protocol/src/mx_eye_protocol` and targets Python 3.11; other workspace
packages are outside this check. Use
`uv run pyright --verifytypes mx_eye_protocol --ignoreexternal` to check public
API type completeness without evaluating external dependencies.

## Delay readouts

| Readout | Definition |
|---|---|
| Processing | Tracking-end minus tracking-start, measured on tracker |
| Acquisition → send | Send minus host read-return timestamp |
| Network | Receiver receipt minus send, after clock-offset correction |
| Arrival age | Receiver receipt minus acquisition, after correction |
| Age now | Current receiver time minus acquisition, after correction |

The command server performs four-timestamp synchronization on the same port. The SDK takes eight
probes, selects the lowest round-trip time, and repeats every 15 seconds. The
estimate expires after 45 seconds without a successful sync. This works with
separate monotonic clocks and does not assume identical boot times or wall clocks.

One-way delay is an **estimate**: asymmetric network paths and clock drift are
not observable exactly from this exchange. Half the minimum RTT indicates the
scale of timing ambiguity under the symmetric-path model, not a calibrated
confidence interval. Negative estimates are not silently clamped away.

The acquisition timestamp is taken **immediately after OpenCV returns a frame**.
Exposure, USB transfer, and buffering before that point are excluded. The
application cannot measure true eye-motion-to-task latency with this camera API.
A hardware-timestamped camera/backend would be needed to improve that boundary.

## Provenance

- Tracking extracted from `mxbi_pupil_cr_tracker_video_v13.py`: original threshold,
  connected-component/ellipse, continuity, pair geometry and reacquisition logic.
- A 500-frame synthetic comparison against v13 produced identical pupil/CR
  detections, including loss and reacquisition. Template center arithmetic was
  corrected to use the pixel center consistently; display work was removed from
  the tracking path.
- Transport design follows the earlier v4 TCP/UDP and clock-sync experiments;
  production code uses standard sockets rather than ZeroMQ.
- No physical-camera or real-marmoset-video validation was possible in this session.

The GUI and SDK still need a run with your camera and video on the target machine.

Reference documentation: [Python multiprocessing](https://docs.python.org/3/library/multiprocessing.html),
[OpenCV camera/video I/O](https://docs.opencv.org/4.x/d8/dfe/classcv_1_1VideoCapture.html),
[OpenCV VideoWriter](https://docs.opencv.org/4.x/dd/d9e/classcv_1_1VideoWriter.html),
[Qt threads](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QThread.html).
