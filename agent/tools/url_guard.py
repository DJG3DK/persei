"""SSRF guard for any URL an LLM gets to choose.

`browse_page` drives a real headless browser on the host, with host network
access, on a URL the model supplies. Before this existed there was no check
at all, and both of these worked (verified live 2026-08-24):

    file:///etc/hostname     -> returned the host's hostname
    http://127.0.0.1:4101/   -> reached the review service's control port

That control port is unauthenticated precisely because "localhost only" was
treated as the boundary, and `file://` reads anything the process can read --
including this agent's own .env. Even driven only by the operator, a page the
agent browses can carry an injected "now fetch file:///..." instruction and
the contents land in the transcript (and from there in traces). For the
public demo bot, where the prompt comes from the internet, it is the whole
ballgame.

Three checks, because any one alone is bypassable:

 1. Scheme allow-list. `file:`, `data:`, `gopher:`, `view-source:` and
    friends have no business here; only http/https.
 2. Resolve-then-validate. Checking the hostname string is useless --
    `evil.example` can simply have an A record of 127.0.0.1. Every address
    the name resolves to must be publicly routable.
 3. Re-validate on redirect. A public URL that 302s to 169.254.169.254
    defeats an entry-only check, so the caller re-runs this per hop (see
    guarded_route_handler, which Playwright invokes for every request the
    page makes, redirects and subresources included).
"""

import ipaddress
import socket
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from playwright.async_api import Request, Route

ALLOWED_SCHEMES = {"http", "https"}


class UnsafeUrlError(Exception):
    """Raised for a URL that must not be fetched."""


def _is_blocked_address(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return True  # unparseable -- refuse rather than guess
    # Explicit categories first -- loopback, RFC1918, link-local
    # (169.254.169.254 cloud metadata), unique-local, multicast, reserved,
    # unspecified. The trailing `not is_global` then catches anything not
    # enumerated here, so a category nobody thought of still fails closed. is_global alone is not sufficient: Python
    # reports multicast (224.0.0.0/4) as is_global=True, so relying on it
    # would have let those through -- caught by this module's own tests.
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True
    # IPv4-mapped IPv6 (::ffff:127.0.0.1) is loopback wearing an IPv6 hat --
    # unwrap and re-test the embedded address rather than trusting the outer.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return _is_blocked_address(str(mapped))
    return not ip.is_global


async def assert_public_url(raw_url: str) -> str:
    """Returns the URL unchanged, or raises UnsafeUrlError.

    Async only so callers can await it uniformly; DNS resolution itself is
    a blocking call run in a thread.
    """
    import asyncio

    try:
        parts = urlsplit(raw_url)
    except ValueError as e:
        raise UnsafeUrlError(f"not a valid URL: {raw_url!r} ({e})") from e

    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrlError(
            f"blocked URL scheme {scheme or '(none)'!r} -- only http and https can be fetched. "
            f"Local files and internal services are not reachable from this tool."
        )

    host = (parts.hostname or "").strip("[]")
    if not host:
        raise UnsafeUrlError(f"no hostname in URL: {raw_url!r}")

    # A literal IP needs no lookup -- check it directly.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if _is_blocked_address(host):
            raise UnsafeUrlError(f"blocked: {raw_url!r} points at a non-public address")
        return raw_url

    port = parts.port or (443 if scheme == "https" else 80)
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise UnsafeUrlError(f"could not resolve {host!r}: {e}") from e

    # An inet sockaddr's host is always a str; str() pins that for the type checker.
    addresses = {str(info[4][0]) for info in infos}
    if not addresses:
        raise UnsafeUrlError(f"{host!r} resolved to no addresses")
    # EVERY address must be public: a name with both a public and a private
    # record would otherwise be a coin flip at connect time.
    blocked = sorted(a for a in addresses if _is_blocked_address(a))
    if blocked:
        raise UnsafeUrlError(
            f"blocked: {host!r} resolves to a non-public address ({blocked[0]})"
        )
    return raw_url


def make_route_guard(on_block: Callable[[str, str], object] | None = None,
                     allow_origin: str | None = None) -> Callable[["Route", "Request"], Awaitable[None]]:
    """A Playwright route handler that applies the same check to EVERY
    request a page makes -- the initial navigation, each redirect hop, and
    every subresource.

    Checking only the URL passed to goto() is not enough: Playwright follows
    redirects internally, so a public URL that 302s to an internal one would
    never be re-examined by the caller.

    `allow_origin` is the one deliberate exception, for previewing an app the
    agent just started: a loopback origin THIS PROCESS chose, on a port it
    allocated, for a container it launched. The model supplies a command and a
    path, never a host -- so this does not give it a way to reach 127.0.0.1:4101
    or a cloud metadata service, which is what the rest of this module is for.
    Exactly one origin, matched whole, and everything else still goes through
    the public check.
    """

    async def handler(route: "Route", request: "Request") -> None:
        if allow_origin and _same_origin(request.url, allow_origin):
            await route.continue_()
            return
        try:
            await assert_public_url(request.url)
        except UnsafeUrlError as e:
            if on_block is not None:
                on_block(request.url, str(e))
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    return handler


def _same_origin(url: str, origin: str) -> bool:
    """Scheme, host and port all equal. Compared as parsed parts rather than
    by prefix: "http://127.0.0.1:8080" is a prefix of
    "http://127.0.0.1:8080.evil.test" and that is exactly the trick."""
    from urllib.parse import urlsplit

    def parts(u: str):
        # .port raises on a malformed authority ("8080.evil.test"), which is
        # itself the answer: not this origin.
        p = urlsplit(u)
        try:
            return (p.scheme, p.hostname, p.port)
        except ValueError:
            return None

    a, b = parts(url), parts(origin)
    return a is not None and a == b
