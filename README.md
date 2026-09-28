# mx_eye

English | [中文](README.zh.md)

First integrated MXBI pupil / corneal-reflection tracker, with a separate receiver SDK and client.
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

Click **Start** in the tracker, then **Connect** in the client: the SDK's
data port exists only while a session is publishing, so connecting first reports
a refused connection. The client's **Start tracker** and **Stop tracker**
buttons control the same session over the control port; START is idempotent when
a session is already running.

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

The SDK is the `py-mx-eye` package; `uv sync --all-extras` installs it. It owns
no thread: every call runs on the caller's thread, so a consumer that has a
thread of its own stays in control of it.

```python
from py_mx_eye import MxEye, MxEyeConfig

# The with block opens the sample stream and always releases it again.
with MxEye(MxEyeConfig(host="127.0.0.1")) as eye:
    for sample in eye.read():  # wait on this thread for each sample
        x, y = sample.frame.payload.x, sample.frame.payload.y
```

`MxEyeConfig` is a frozen dataclass holding the connection parameters, so the
constructor itself stays one argument long. `with` scopes only the sample
stream, which `connect()` opens (the data port exists only while a session runs)
and `close()` releases; acquisition stays explicit through `start()`/`stop()`,
so leaving the block never stops the tracker.

`read()` yields only samples that are still current measurements: lost, stale
and validity-failing samples are skipped while it waits. `timeout` bounds each
wait rather than the whole loop, and the loop ends when a wait expires, so
`read(0)` is a non-blocking drain (what a GUI timer wants), a finite timeout also
ends the loop after that much silence, and the default waits indefinitely. Pass
`max_age_ms=None, require_valid=False` to yield whatever arrives next, for
plotting or diagnostics.

Errors are exceptions rather than silent state: `connect()` raises when the
tracker is not publishing, a closed or broken stream raises `ConnectionError`
(connect again to resume), and a malformed frame raises `ValueError`. Coordinates
are uncalibrated source-image pixels. A runnable example:
`uv run python -m mx_eye_client.receive_minimal`. More detail in AGENTS.md.
