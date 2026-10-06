"""Bounded ZeroMQ publication for tracking samples."""

import zmq


class Publisher:
    """Publish without waiting for subscribers; full subscriber queues lose samples."""

    def __init__(self, host, port):
        self._context = zmq.Context()
        self._sock = self._context.socket(zmq.PUB)
        try:
            self._sock.setsockopt(zmq.LINGER, 0)
            self._sock.setsockopt(zmq.SNDHWM, 64)
            self._sock.bind(f"tcp://{host}:{port}")
        except Exception:
            self.close()
            raise

    def send(self, packet):
        """Return local send errors; ZeroMQ does not report subscriber drops."""
        try:
            self._sock.send(packet, flags=zmq.NOBLOCK)
        except zmq.ZMQError:
            return 1
        return 0

    def close(self):
        self._sock.close(linger=0)
        self._context.term()
