"""Small password-capable SSH local forwarder used by start_local_frontend.ps1."""

from __future__ import annotations

import os
import select
import socketserver
import sys

import paramiko


class ForwardHandler(socketserver.BaseRequestHandler):
    ssh_transport: paramiko.Transport
    remote_host: str
    remote_port: int

    def handle(self) -> None:
        channel = self.ssh_transport.open_channel(
            "direct-tcpip",
            (self.remote_host, self.remote_port),
            self.request.getpeername(),
        )
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

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        ssh_host,
        port=int(ssh_port),
        username=ssh_user,
        password=password,
        look_for_keys=False,
        allow_agent=False,
        timeout=20,
        auth_timeout=20,
        banner_timeout=20,
    )
    transport = client.get_transport()
    if transport is None:
        raise RuntimeError("SSH transport was not established")
    transport.set_keepalive(30)

    handler = type(
        "ConfiguredForwardHandler",
        (ForwardHandler,),
        {
            "ssh_transport": transport,
            "remote_host": remote_host,
            "remote_port": int(remote_port),
        },
    )
    try:
        with ThreadingForwardServer(("127.0.0.1", int(local_port)), handler) as server:
            print(f"127.0.0.1:{local_port} -> {remote_host}:{remote_port}", flush=True)
            server.serve_forever(poll_interval=0.5)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
