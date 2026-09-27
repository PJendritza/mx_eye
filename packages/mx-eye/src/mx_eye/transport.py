"""Bounded standard-library transport for tracking samples."""

import socket


def listen(host, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((str(host), int(port)))
    sock.listen(8)
    return sock


class Publisher:
    """Never waits on a receiver. Disconnects slow/partial-write clients.

    A slow consumer gets a fresh connection rather than an unbounded application
    queue of old samples. The SDK checks session/sequence and detects gaps.
    """

    def __init__(self, host, port):
        self.server = listen(host, port)
        self.server.setblocking(False)
        self.clients = []

    def send(self, packet):
        for _ in range(8):
            try:
                client, _ = self.server.accept()
                client.setblocking(False)
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                client.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
                if len(self.clients) < 8:
                    self.clients.append(client)
                else:
                    client.close()
            except BlockingIOError:
                break
        errors = 0
        for client in list(self.clients):
            try:
                if client.send(packet) != len(packet):
                    raise ConnectionError("Partial send")
            except OSError:
                self.clients.remove(client)
                client.close()
                errors += 1
        return errors

    def close(self):
        for client in self.clients:
            client.close()
        self.server.close()
