"""Minimal logging HTTPS proxy for testing whether sbx traffic flows through host proxies.

Usage:
    python logging_proxy.py [port]

Listens for HTTP CONNECT requests (used by HTTPS clients going through a proxy),
logs them, and tunnels the connection to the real destination. Non-CONNECT requests
are logged and forwarded as-is.
"""

import select
import socket
import sys
import threading
from datetime import datetime, timezone


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def tunnel(client: socket.socket, remote: socket.socket) -> None:
    """Bidirectional byte shuttle between client and remote."""
    sockets = [client, remote]
    try:
        while True:
            readable, _, _ = select.select(sockets, [], [], 30)
            if not readable:
                break
            for s in readable:
                data = s.recv(65536)
                if not data:
                    return
                target = remote if s is client else client
                target.sendall(data)
    except OSError:
        pass
    finally:
        client.close()
        remote.close()


def handle_client(client: socket.socket, addr: tuple) -> None:
    try:
        data = client.recv(65536)
        if not data:
            client.close()
            return

        first_line = data.split(b"\r\n")[0].decode(errors="replace")
        method, target, *_ = first_line.split()

        log(f"{addr[0]}:{addr[1]}  {method}  {target}")

        if method == "CONNECT":
            host, port = target.split(":")
            port = int(port)
            remote = socket.create_connection((host, port), timeout=10)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            tunnel(client, remote)
        else:
            # Plain HTTP forward — parse host from target or Host header
            from urllib.parse import urlparse

            parsed = urlparse(target)
            host = parsed.hostname
            port = parsed.port or 80
            remote = socket.create_connection((host, port), timeout=10)
            remote.sendall(data)
            while True:
                chunk = remote.recv(65536)
                if not chunk:
                    break
                client.sendall(chunk)
            remote.close()
            client.close()
    except Exception as e:
        log(f"ERROR handling {addr}: {e}")
        client.close()


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18080
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen(128)
    log(f"Logging proxy listening on :{port}")
    log("Waiting for connections... (Ctrl+C to stop)")

    try:
        while True:
            client, addr = server.accept()
            threading.Thread(
                target=handle_client, args=(client, addr), daemon=True
            ).start()
    except KeyboardInterrupt:
        log("Shutting down.")
    finally:
        server.close()


if __name__ == "__main__":
    main()
