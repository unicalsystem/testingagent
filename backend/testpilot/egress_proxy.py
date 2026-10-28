"""A per-run filtering forward proxy that all Chromium traffic is forced through.

Why: Playwright's request interception does not see redirect hops, and Chromium resolves DNS
itself, so URL checks alone cannot stop a redirect (or DNS rebinding) to an internal address.
Every connection the browser makes -- navigations, every redirect hop, sub-resources,
WebSockets -- arrives here first. The proxy checks the host against the run's allowlist,
resolves it, rejects private/metadata addresses, and connects to the vetted IP itself.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from urllib.parse import urlsplit

from .urlguard import Policy, TargetRejected, check_ip, normalize_host, resolve

log = logging.getLogger(__name__)
MAX_HEADER = 64 * 1024


class EgressProxy:
    def __init__(self, policy: Policy, allowed_ports: set[int], on_block: Callable[[str], None] | None = None):
        self.policy = policy
        self.allowed_ports = allowed_ports
        self.on_block = on_block or (lambda m: None)
        self.blocked: list[str] = []
        self.port = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.base_events.Server | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    # ---- lifecycle ---------------------------------------------------------
    def start(self) -> int:
        self._thread = threading.Thread(target=self._run, name="egress-proxy", daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("egress proxy failed to start")
        return self.port

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def boot():
            self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
            self.port = self._server.sockets[0].getsockname()[1]
            self._ready.set()

        self._loop.run_until_complete(boot())
        self._loop.run_forever()

    def stop(self) -> None:
        if not self._loop:
            return

        async def shutdown():
            if self._server:
                self._server.close()
            for t in asyncio.all_tasks():
                if t is not asyncio.current_task():
                    t.cancel()

        fut = asyncio.run_coroutine_threadsafe(shutdown(), self._loop)
        try:
            fut.result(5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(5)

    # ---- policy ------------------------------------------------------------
    def _vet(self, host: str, port: int) -> list[str]:
        host = normalize_host(host)
        if host not in self.policy.allowed_hosts:
            raise TargetRejected(f"host '{host}' is not on the authorised target allowlist")
        if port not in self.allowed_ports:
            raise TargetRejected(f"port {port} is not allowed for test targets")
        ips = resolve(host, port)
        for ip in ips:  # reject if ANY address is disallowed (defeats mixed-record tricks)
            check_ip(ip, private_ok=host in self.policy.private_ok)
        # IPv4 first: the compose networks are IPv4-only; IPv6 is still tried as a fallback.
        return sorted(ips, key=lambda a: (":" in a, a))

    async def _connect(self, ips: list[str], port: int):
        last: Exception | None = None
        for ip in ips:  # connect only to addresses vetted above (no re-resolution)
            try:
                return await asyncio.wait_for(asyncio.open_connection(ip, port), 10)
            except (OSError, asyncio.TimeoutError) as e:
                last = e
        raise ConnectionError(f"could not connect to any vetted address: {last}")

    def _block(self, msg: str) -> None:
        self.blocked.append(msg)
        log.warning("egress blocked: %s", msg)
        try:
            self.on_block(msg)
        except Exception:
            pass

    # ---- protocol ----------------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_w = None
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
            if len(head) > MAX_HEADER:
                raise ValueError("header too large")
            lines = head.decode("latin-1").split("\r\n")
            method, target, version = lines[0].split(" ", 2)
            if method.upper() == "CONNECT":
                host, _, port_s = target.rpartition(":")
                port = int(port_s)
                try:
                    ips = self._vet(host, port)
                except TargetRejected as e:
                    self._block(f"CONNECT {target}: {e}")
                    writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    await writer.drain()
                    return
                try:
                    ur, upstream_w = await self._connect(ips, port)
                except ConnectionError:
                    writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    await writer.drain()
                    return
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await self._pipe(reader, writer, ur, upstream_w)
                return

            # Plain HTTP via absolute-form request target.
            parts = urlsplit(target)
            if parts.scheme.lower() != "http" or not parts.hostname:
                self._block(f"{method} {target[:200]}: unsupported proxy request")
                writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            port = parts.port or 80
            try:
                ips = self._vet(parts.hostname, port)
            except TargetRejected as e:
                self._block(f"{method} {target[:200]}: {e}")
                body = f"Blocked by TestAgent egress policy: {e}".encode()
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nX-TestAgent-Blocked: 1\r\nConnection: close\r\n"
                             + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
                await writer.drain()
                return
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            out = [f"{method} {path} {version}"]
            for h in lines[1:]:
                if not h:
                    continue
                k = h.split(":", 1)[0].strip().lower()
                if k in ("proxy-connection", "connection", "keep-alive", "proxy-authorization"):
                    continue
                out.append(h)
            out.append("Connection: close")  # one request per upstream connection: no cross-host reuse
            try:
                ur, upstream_w = await self._connect(ips, port)
            except ConnectionError:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            upstream_w.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1"))
            await upstream_w.drain()
            await self._pipe(reader, writer, ur, upstream_w, close_after_response=True)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, ValueError, OSError) as e:
            log.debug("proxy connection ended: %s", e)
        except asyncio.CancelledError:
            pass
        finally:
            for w in (writer, upstream_w):
                if w is not None:
                    try:
                        w.close()
                    except Exception:
                        pass

    async def _pipe(self, cr, cw, ur, uw, close_after_response: bool = False) -> None:
        async def copy(src, dst):
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, OSError, asyncio.CancelledError):
                pass

        up = asyncio.create_task(copy(cr, uw))
        down = asyncio.create_task(copy(ur, cw))
        if close_after_response:
            await down  # upstream closes after one response (Connection: close)
            up.cancel()
        else:
            done, pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
