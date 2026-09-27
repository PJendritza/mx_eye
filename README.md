# mx_eye

First integrated MXBI pupil / corneal-reflection tracker, with a separate receiver SDK and client.
Python 3.10+; desktop UI for Windows/Linux.

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

Click **Start** in the tracker. In the client, click **Connect**; its
**Start tracker** and **Stop tracker** buttons control the same session.
START is idempotent when a session is already running. Alternatively, connect the
client first and start the session from there.

## SDK

The SDK is the `py-mx-eye` package; `uv sync --all-extras` installs it.

```python
from py_mx_eye import Client

with Client("127.0.0.1") as eye:
    eye.start()
    # Inside your behavioral-task loop:
    sample = eye.latest(max_age_ms=50)
    if sample is not None:
        x, y = sample.frame.payload.x, sample.frame.payload.y
    # At the end of the session:
    eye.stop()
```

`latest()` returns None until tracking and clock synchronization are ready, and
coordinates are uncalibrated source-image pixels. A runnable example:
`uv run python -m mx_eye_client.receive_minimal`. More detail in AGENTS.md.

Sampling fields are defined once by `mx_eye_protocol.TrackingPayload`.
`TrackingPayload.flags` uses `TrackingFlags` (`IntFlag`), for example
`TrackingFlags.VALID | TrackingFlags.PUPIL`; `TrackingFlags.NONE` means no flags.
`DataFrame` wraps that payload with `magic`, `message_type`, and `length`.
Both dataclasses live in `mx_eye_protocol/data_frame.py`.
The SDK returns `Sample(frame=..., receive_ns=..., ...)`: use
`sample.frame.payload.x` and `sample.frame.payload.valid` for tracking data,
`sample.frame.length` for encoded payload size, and `sample.age_ms` for reception timing.
The former `Packet` type and flat `Sample` sampling attributes have been removed.

The binary header is little-endian `<4sBI`: magic `MXEY`, message type
(`DATA=1`, `CMD=2`), and a uint32 payload byte count excluding the header.
The DATA payload is `<7Qq8fB` (97 bytes); its flags are the final byte.
A DATA frame is therefore 106 bytes. `DataFrame.from_payload(payload)` builds
its header; `frame.encode()` serializes it without protocol validation, and
`frame.frame_size` reports header size plus declared payload length.
Decoding and incoming-header validation belong to the receiver; the SDK implements
them in its private `_decoder.py` module. TCP reception reads the header before
waiting for the declared payload, handling split and coalesced frames.
Invalid headers disconnect the TCP stream; invalid UDP datagrams are dropped.
Unknown types, unsupported CMD frames, and invalid DATA lengths are rejected
before buffering their declared bodies.

This replaces the former 104-byte v1 sample protocol: update tracker and SDK
together. UDP samples use the same new envelope. Recording CSV columns and
precision are unchanged. The binary CMD type remains reserved.
