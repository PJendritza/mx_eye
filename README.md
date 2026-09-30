# mx_eye

English | [中文](README.zh.md)

Integrated MXBI pupil / corneal-reflection tracker, with a separate receiver SDK and client.
Python 3.11+; desktop UI for Windows/Linux.

The repository is a uv workspace with four packages: `mx-eye` (the tracker),
`mx-eye-client` (the remote client), `py-mx-eye` (the Python SDK) and
`mx-eye-protocol` (the wire formats they share).

## Install and run

Install everything once, in the repository root:

```bash
uv sync --all-extras
```

Start the tracker and, in a second terminal, the remote client:

```bash
uv run mx-eye
uv run mx-eye-receiver
```

For a camera-free first check:

```bash
uv run mx-eye --demo
```

On Raspberry Pi OS/Debian, install `v4l-utils` for USB camera discovery and
exposure/gain controls. Install `ffmpeg` to save a compatible V4L2 MJPEG camera's
original compressed frames without re-encoding (`sudo apt install v4l-utils
ffmpeg`). When that path is unavailable, the recording worker saves decoded
full-resolution frames using the selected codec. Camera Settings shows driver
modes and exposure controls; ACQ reports the rate actually delivered.

The **Pupil method** selector offers the original threshold detector,
Starburst-style radial edges, edge/ellipse fitting, and adaptive thresholding.
The method selected at session start is saved in `config.json`; `tracking.csv`
retains its established 17-column format. **Reason** shows the latest rejected pupil/CR sample on
the right side of the rate bar. **Suspend displays** reduces preview/plot work
while acquisition, tracking, network output and recording continue. Settings
can import the previous desktop app's version 1 JSON configuration; the old
`sync_port` setting is retired because clocks now synchronize through the OS.

Click **Connect** in the client before or after starting the tracker. The SDK
subscribes asynchronously and reconnects when publication resumes. The client's
**Start tracker** and **Stop tracker** buttons use ZeroMQ REQ/REP; samples use
PUB/SUB. START is idempotent when a session is already running.

## Clock synchronization

The eyetracker and the consumer device share one time base through system-level
NTP; delay and age readouts compare packet timestamps directly. On a direct
Ethernet link with the consumer device as NTP server, run this once on the
mx_eye host (Raspberry Pi OS/Debian, chrony):

```bash
sudo scripts/install-chrony-client.sh 192.168.50.1   # consumer-device address
scripts/check-chrony-client.sh                       # wait and verify
```

More detail in AGENTS.md.

## SDK

The SDK is the `py-mx-eye` package; `uv sync --all-extras` installs it. It creates
no Python background receiver thread: calls run on the caller's thread, while
libzmq uses internal I/O threads. Open, read and close a sample stream on the
same thread; control calls use independent REQ sockets.

```python
from py_mx_eye import MxEye, MxEyeConfig

# The with block opens the sample stream and always releases it again.
with MxEye(MxEyeConfig(host="127.0.0.1")) as eye:
    for sample in eye.read():  # wait on this thread for each sample
        x, y = sample.frame.x, sample.frame.y
```

`MxEyeConfig` is a frozen dataclass holding the connection parameters, so the
constructor itself stays one argument long. `with` scopes only the sample
stream, which `connect()` subscribes to asynchronously and `close()` releases; acquisition stays explicit through `start()`/`stop()`,
so leaving the block never stops the tracker.

`read()` yields only samples that are still current measurements: lost, stale
and validity-failing samples are skipped while it waits. `timeout` bounds each
wait rather than the whole loop, and the loop ends when a wait expires, so
`read(0)` is a non-blocking drain (what a GUI timer wants), a finite timeout also
ends the loop after that much silence, and the default waits indefinitely. Pass
`max_age_ms=None, require_valid=False` to yield whatever arrives next, for
plotting or diagnostics.

`connect()` does not prove the publisher is online; use `read(timeout=...)`
to bound a data wait. Subscription setup and reconnection can lose initial
samples. Queues are bounded to 64 messages per peer on each end; slow consumers
can lose samples without blocking tracking. Malformed data messages are dropped.

The tracker binds PUB on port 5556 and REP on port 5557 by default. Both use TCP.
Each sample is one 97-byte binary message, without an application header. Each
control request/reply is one UTF-8 JSON message. Commands are processed serially:
start/stop wait for their operation result, so other requests queue during startup.
Stop may return `stopping`; query status for completion. Control timeouts raise
`TimeoutError`, do not cancel server execution, and are never automatically retried.

Update tracker and SDK together. Raw TCP/UDP, the old frame header, and the
`transport`, `udp_host`, and `udp_bind` settings have been removed; remove these
keys from saved configurations. Host/bind and the two port settings remain.
Coordinates are uncalibrated source-image pixels. A runnable example:
`uv run python -m mx_eye_client.receive_minimal`. More detail in AGENTS.md.

### GUI controls

Use **File** to open a video or save/load configurations and templates. **Settings**
contains Camera, Network, Recording and View tabs; View controls GUI refresh rate
and overlay visibility independently of acquisition/tracking. View options can be
changed while tracking; source/network/recording settings require a stopped session.

The source controls show Start/Stop for camera and simulation, or Open video for
file playback. Switching source stops the current session. **Record** starts and
stops recording independently; tracking starts without recording. Each recording
gets its own folder, video, timestamps and tracking CSV. Wait for Finalizing to
finish before starting another recording. Recording is disabled for file playback.

**Tools → Timing / performance diagnostics** displays existing rates, latest
tracking-loop duration, skipped frames, send errors, buffer backlog, and GUI timing.
GUI callback timing is collected only while the diagnostics window is visible.
Detailed per-stage profiling and calibration tools are planned separately;
Calibration is currently a menu placeholder.
