"""The headless browser's only way out: a local proxy that connects to the
address it checked.

url_guard's route handler resolves a request's host and checks every
address, then hands the request back to Chromium -- which resolves the host
again, itself, to connect. A name that answers with a public address to the
first lookup and 127.0.0.1 to the second (DNS rebinding, a TTL of zero) passes
the check and reaches loopback anyway. Nothing inside the route handler can
close that: it never owns the connection.

So Chromium is launched behind this proxy, with loopback forced through it
too (Chromium bypasses a proxy for loopback by default). Every request, plain
HTTP or a CONNECT tunnel, arrives here with a hostname; this resolves it ONCE,
applies url_guard's rule to every address, and opens the socket to the
address it just checked. There is no second lookup to rebind.

One exception, the same as url_guard's: the single loopback origin a preview
was started on, matched as host and port.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import socket
from typing import Any
from urllib.parse import urlsplit

# One address policy for both egress paths, so the proxy reuses url_guard's.
# pyright: reportPrivateUsage=false
from agent.tools.url_guard import _is_blocked_address

logger = logging.getLogger("tektonix")

_CHUNK = 64 * 1024
_CONNECT_TIMEOUT_S = 15
_HEAD_TIMEOUT_S = 30
# On the proxy's own refusal, so a caller can tell it from a site's 403.
REFUSED_HEADER = "x-tektonix-egress-refused"

# Hop-by-hop headers that belong to the browser-to-proxy leg only.
_DROP_HEADERS = ("proxy-", "connection:", "keep-alive:")


class EgressBlocked(Exception):
    """A request whose host is, or resolves to, a non-public address."""


def _host_port(authority: str, default_port: int) -> tuple[str, int]:
    parts = urlsplit(f"//{authority}")
    host = (parts.hostname or "").strip("[]")
    if not host:
        raise ValueError(f"no host in {authority!r}")
    return host, parts.port or default_port


def _origin_host_port(origin: str) -> tuple[str, int]:
    p = urlsplit(origin)
    return (p.hostname or "").lower(), p.port or (443 if p.scheme == "https" else 80)


class EgressProxy:
    """`async with EgressProxy() as proxy:` then launch the browser with
    `proxy.launch_kwargs()`. Stopping it closes every connection it holds."""

    def __init__(self, allow_origin: str | None = None):
        self._allow = _origin_host_port(allow_origin) if allow_origin else None
        self._server: asyncio.base_events.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self.blocked: list[str] = []

    async def __aenter__(self) -> EgressProxy:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        for w in list(self._writers):
            w.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._server.wait_closed(), 5)

    @property
    def url(self) -> str:
        assert self._server is not None
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def launch_kwargs(self) -> dict[str, Any]:
        # <-loopback> removes Chromium's implicit loopback bypass, so
        # http://127.0.0.1/ and http://localhost/ come here like anything else.
        return {"proxy": {"server": self.url, "bypass": "<-loopback>"}}

    # -- the check ---------------------------------------------------------

    async def resolve(self, host: str, port: int) -> str:
        """The one address this request may connect to. Every address the
        name has must be public, as in url_guard.assert_public_url."""
        if self._allow is not None and (host.lower(), port) == self._allow:
            return host
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if _is_blocked_address(host):
                raise EgressBlocked(f"{host} is not a public address")
            return host
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
        except socket.gaierror as e:
            raise EgressBlocked(f"could not resolve {host}: {e}") from e
        # An inet sockaddr's host is always a str; str() pins that for the type checker.
        addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
        if not addresses:
            raise EgressBlocked(f"{host} resolved to no addresses")
        bad = [a for a in addresses if _is_blocked_address(a)]
        if bad:
            raise EgressBlocked(f"{host} resolves to a non-public address ({bad[0]})")
        return addresses[0]

    async def connect(self, address: str, port: int):
        return await asyncio.wait_for(asyncio.open_connection(address, port), _CONNECT_TIMEOUT_S)

    async def _open(self, host: str, port: int):
        address = await self.resolve(host, port)
        return await self.connect(address, port)

    # -- the proxy ---------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        upstream_writer = None
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _HEAD_TIMEOUT_S)
            request_line, *header_lines = head[:-4].decode("latin-1").split("\r\n")
            method, target, version = request_line.split(" ", 2)
            if method.upper() == "CONNECT":
                host, port = _host_port(target, 443)
                upstream_reader, upstream_writer = await self._open(host, port)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                parts = urlsplit(target)
                if parts.scheme != "http" or not parts.hostname:
                    await _reply(writer, 400, "absolute http:// URL required")
                    return
                host, port = parts.hostname.strip("[]"), parts.port or 80
                upstream_reader, upstream_writer = await self._open(host, port)
                path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
                headers = [h for h in header_lines if h and not h.lower().startswith(_DROP_HEADERS)]
                # One request per upstream connection: a kept-alive browser
                # connection could otherwise carry its next request, for a
                # different host, down this socket unchecked.
                headers.append("Connection: close")
                request_head = f"{method} {path} {version}\r\n" + "".join(f"{h}\r\n" for h in headers) + "\r\n"
                upstream_writer.write(request_head.encode("latin-1"))
                await upstream_writer.drain()
            await _splice(reader, writer, upstream_reader, upstream_writer)
        except EgressBlocked as e:
            self.blocked.append(str(e))
            logger.warning("browser egress refused: %s", e)
            await _reply(writer, 403, f"blocked: {e}")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError, UnicodeError):
            await _reply(writer, 400, "malformed proxy request")
        except (OSError, TimeoutError) as e:
            await _reply(writer, 502, f"could not connect: {type(e).__name__}")
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
            writer.close()
            self._writers.discard(writer)


async def _reply(writer: asyncio.StreamWriter, status: int, text: str) -> None:
    reason = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway"}[status]
    body = text.encode("utf-8", "replace")
    marker = f"{REFUSED_HEADER}: 1\r\n" if status == 403 else ""
    with contextlib.suppress(Exception):
        writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain; charset=utf-8\r\n{marker}"
                     f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("latin-1") + body)
        await writer.drain()


async def _pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    with contextlib.suppress(ConnectionError, OSError):
        while data := await src.read(_CHUNK):
            dst.write(data)
            await dst.drain()


async def _splice(a_reader: asyncio.StreamReader, a_writer: asyncio.StreamWriter,
                  b_reader: asyncio.StreamReader, b_writer: asyncio.StreamWriter) -> None:
    """Bytes both ways until either side closes, then both are closed."""
    tasks = {asyncio.create_task(_pump(a_reader, b_writer)), asyncio.create_task(_pump(b_reader, a_writer))}
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
