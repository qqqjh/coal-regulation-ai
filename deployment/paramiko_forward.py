"""Small password-capable SSH local forwarder used by start_local_frontend.ps1."""

from __future__ import annotations

import os
import select
import socketserver
import sys
import threading

import paramiko


class SSHTransportManager:
    """Keep the local listener alive while transparently reconnecting SSH."""

    def __init__(self, host: str, port: int, user: str, password: str):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self._client: paramiko.SSHClient | None = None
        self._lock = threading.Lock()

    def _close_unlocked(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def get_transport(self) -> paramiko.Transport:
        with self._lock:
            if self._client is not None:
                transport = self._client.get_transport()
                if transport is not None and transport.is_active() and transport.is_authenticated():
                    return transport
                self._close_unlocked()

            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                self.host,
                port=self.port,
                username=self.user,
                password=self.password,
                look_for_keys=False,
                allow_agent=False,
                timeout=20,
                auth_timeout=20,
                banner_timeout=20,
            )
            transport = client.get_transport()
            if transport is None:
                client.close()
                raise RuntimeError("SSH transport was not established")
            transport.set_keepalive(15)
            self._client = client
            return transport

    def invalidate(self, transport: paramiko.Transport) -> None:
        with self._lock:
            current = self._client.get_transport() if self._client is not None else None
            if current is transport:
                self._close_unlocked()

    def close(self) -> None:
        with self._lock:
            self._close_unlocked()


class ForwardHandler(socketserver.BaseRequestHandler):
    transport_manager: SSHTransportManager
    remote_host: str
    remote_port: int

    def handle(self) -> None:
        channel = None
        for _ in range(2):
            transport = self.transport_manager.get_transport()
            try:
                channel = transport.open_channel(
                    "direct-tcpip",
                    (self.remote_host, self.remote_port),
                    self.request.getpeername(),
                )
                break
            except (paramiko.SSHException, OSError):
                self.transport_manager.invalidate(transport)
        if channel is None:
            return
        try:
            while True:
                readable, _, _ = select.select([self.request, channel], [], [])
                if self.request in readable:
                    data = self.request.recv(65536)
                    if not data:
                        break
                    channel.sendall(data)
                if channel in readable:
                    data = channel.recv(65536)
                    if not data:
                        break
                    self.request.sendall(data)
        finally:
            channel.close()


class ThreadingForwardServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    if len(sys.argv) != 7:
        print(
            "usage: paramiko_forward.py SSH_HOST SSH_PORT SSH_USER "
            "LOCAL_PORT REMOTE_HOST REMOTE_PORT",
            file=sys.stderr,
        )
        return 2
    ssh_host, ssh_port, ssh_user, local_port, remote_host, remote_port = sys.argv[1:]
    password = os.getenv("COAL_SSH_PASSWORD", "")
    if not password:
        print("COAL_SSH_PASSWORD is not set", file=sys.stderr)
        return 2

    manager = SSHTransportManager(
        ssh_host,
        int(ssh_port),
        ssh_user,
        password,
    )
    manager.get_transport()

    handler = type(
        "ConfiguredForwardHandler",
        (ForwardHandler,),
        {
            "transport_manager": manager,
            "remote_host": remote_host,
            "remote_port": int(remote_port),
        },
    )
    try:
        with ThreadingForwardServer(("127.0.0.1", int(local_port)), handler) as server:
            print(f"127.0.0.1:{local_port} -> {remote_host}:{remote_port}", flush=True)
            server.serve_forever(poll_interval=0.5)
    finally:
        manager.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
