"""Classic serial REP service; its worker thread owns all ZeroMQ resources."""

import threading
from collections.abc import Callable

import zmq
from mx_eye_protocol.control import ControlReply, ControlRequest


class ControlServer:
    """Receive, execute and reply before accepting the next command."""

    def __init__(
        self,
        address: tuple[str, int],
        dispatch: Callable[[ControlRequest], ControlReply],
    ) -> None:
        self.server_address = address
        self.dispatch = dispatch
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: Exception | None = None
        self._thread = threading.Thread(
            target=self._serve, daemon=True, name="mx-eye control"
        )

    def start(self) -> None:
        self._thread.start()
        self._ready.wait()
        if self._error is not None:
            self._thread.join()
            raise self._error

    def _serve(self) -> None:
        try:
            with zmq.Context() as context, context.socket(zmq.REP) as sock:
                sock.setsockopt(zmq.LINGER, 0)
                sock.setsockopt(zmq.SNDTIMEO, 1000)
                host, port = self.server_address
                if port == 0:
                    port = sock.bind_to_random_port(f"tcp://{host}")
                    self.server_address = host, port
                else:
                    sock.bind(f"tcp://{host}:{port}")
                self._ready.set()
                while not self._stop.is_set():
                    if not sock.poll(100, zmq.POLLIN):
                        continue
                    parts = sock.recv_multipart()
                    try:
                        if len(parts) != 1:
                            raise ValueError("Expected one control request frame")
                        request = ControlRequest.model_validate_json(parts[0])
                        reply = self.dispatch(request)
                    except Exception as exc:  # noqa: BLE001 - serialize command failures
                        reply = ControlReply(
                            ok=False, error=str(exc) or type(exc).__name__
                        )
                    sock.send_string(reply.model_dump_json(exclude_none=True))
        except Exception as exc:  # noqa: BLE001 - report worker startup/runtime failure
            self._error = exc
        finally:
            self._ready.set()

    def close(self) -> None:
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.close()
