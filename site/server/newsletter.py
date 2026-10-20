"""The newsletter signup behind perseiagent.ai.

Deliberately not part of the agent. It belongs to the landing page -- the same
reasoning that moved the page out of the product: a self-hosted Persei has
no business shipping an endpoint that writes to perseiagent.ai's mailing list.
`site/` is excluded from the release tarball, and this goes with it.

It does one thing: take a name and an address from the form on the landing
page and write them down. Nothing sends yet, on purpose.

No JavaScript at the other end
------------------------------
The landing page ships zero JavaScript, so this is a plain HTML form POST.
That shapes the whole interface:

  * form-encoded input, not JSON;
  * the answer is a 303 to a real page, because a status code with a JSON body
    is not something a browser would show anyone;
  * every outcome has a page, including failure, since there is no script to
    render an inline error.

A duplicate signup is a SUCCESS. The person's intent was "put me on the list",
they are on the list, and an error would send them away thinking it had not
worked. It is also a no-op: the row keeps its name, and an address that
unsubscribed stays unsubscribed. There is no double opt-in, so a signup is
not proof it came from the address's owner, and letting it re-subscribe or
rename somebody who left would let anyone do that to anyone (2026-09-29
audit, S1).

On storing addresses
--------------------
Name, address, when, and a token for the unsubscribe link the first issue will
need. Addresses are never written to the log -- a log line is the easiest way
for a mailing list to leak, and this one would leak into a file that gets
tailed over someone's shoulder.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager

from urllib.parse import urlsplit

import httpx
import psycopg
from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from psycopg_pool import AsyncConnectionPool

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("newsletter")

def _load_env_file(path: str) -> None:
    """Read KEY=value lines into the environment, without overwriting.

    A few lines of parsing rather than a dotenv dependency: this service
    exists to have very few. A missing file is fine -- the values can come
    from the process environment instead.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))
    except OSError:
        pass


_HERE = os.path.dirname(os.path.abspath(__file__))
# Its own file first, so the service is self-contained under pm2 rather than
# depending on whatever environment happened to start it.
_load_env_file(os.path.join(_HERE, ".env"))

# Its own table, and its own reason to exist. The DSN falls back to the box's
# existing database because that is what pg_dump already backs up
# (docs/backup.md); point it somewhere else and nothing here cares.
DSN = os.environ.get("NEWSLETTER_PG_DSN") or os.environ.get("LANGGRAPH_PG_DSN") or ""
SITE_URL = (os.environ.get("SITE_URL") or "https://perseiagent.ai").rstrip("/")
PORT = int(os.environ.get("NEWSLETTER_PORT") or 8300)

OK_URL = f"{SITE_URL}/subscribed/"
FAIL_URL = f"{SITE_URL}/subscribe-failed/"
_SITE_ORIGIN = "{0.scheme}://{0.netloc}".format(urlsplit(SITE_URL)).lower()

# The form has one field people never see (site/src/LandingPage.tsx). A
# submission that fills it came from something reading the markup, not the
# page. Named like a real field on purpose: a bot skips one called "trap".
HONEYPOT_FIELD = "website"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS newsletter_subscribers (
    id SERIAL PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    -- For the unsubscribe link every issue has to carry. Minted now so the
    -- first send does not have to backfill one for everybody.
    unsubscribe_token TEXT UNIQUE NOT NULL,
    -- Nothing sends yet, so nothing is confirmed yet. Double opt-in needs a
    -- mail to confirm WITH; the column is here so adding it later is a write
    -- rather than a migration.
    confirmed_at TIMESTAMPTZ,
    unsubscribed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source TEXT
);
"""

# Deliberately loose. Address syntax is far stranger than any regex people
# write for it, and the only thing that truly validates an address is sending
# to it. This rejects the obviously-not-an-address and lets the rest through.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

# A public HTML form with no CAPTCHA. Five signups from one IP in a minute
# is a person retrying; thirty is a bot filling the table. Same shape as
# agent/rate_limit.py (sliding window, X-Real-IP first) but local -- this
# service must not import the agent. Fail-open on its own errors so a
# limiter bug cannot take the form down. The answer is still a 303: there
# is no JavaScript to render a 429.
_SUBSCRIBE_MAX = 5
_SUBSCRIBE_WINDOW_S = 60
_subscribe_hits: dict[str, list[float]] = defaultdict(list)


def _client_ip(request: Request) -> str:
    # X-Real-IP only when a proxy we control set it (nginx overwrites the
    # header from $remote_addr). X-Forwarded-For is not consulted: without
    # that proxy it is the client's own string, and rotating it would be
    # the whole limiter. The socket peer is the fallback.
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    return request.client.host if request.client else "unknown"


# An IP is only pruned when it comes back, so without this the table keeps one
# entry per address that ever submitted the form -- for the life of the
# process, on a public page, where rotating the source address is the cheapest
# thing an abuser does. Swept opportunistically rather than on a timer: this
# service has no scheduler, and a sweep that runs when the table is already
# small is work nobody needed.
_SWEEP_AT = 10_000


def _sweep(now: float) -> None:
    for ip in [ip for ip, hits in _subscribe_hits.items()
               if not hits or now - hits[-1] >= _SUBSCRIBE_WINDOW_S]:
        del _subscribe_hits[ip]


def _subscribe_allowed(ip: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    try:
        if len(_subscribe_hits) >= _SWEEP_AT:
            _sweep(now)
        recent = [t for t in _subscribe_hits[ip] if now - t < _SUBSCRIBE_WINDOW_S]
        if len(recent) >= _SUBSCRIBE_MAX:
            _subscribe_hits[ip] = recent
            return False
        recent.append(now)
        _subscribe_hits[ip] = recent
        return True
    except Exception:  # noqa: BLE001 -- a limiter bug must not refuse a person
        return True


def reset_subscribe_limiter() -> None:
    """Tests only. The window is process-global."""
    _subscribe_hits.clear()


def _from_another_site(request: Request) -> bool:
    """Whether the browser says this POST came from a page on another site.

    Browsers send Origin on every form POST and Sec-Fetch-Site on every
    request, so a header naming another site is a page somewhere else
    posting to this form with an address that is not its owner's. A missing
    header (an old browser, curl) is no evidence either way and passes: the
    honeypot and the rate limit are what stand in front of those (2026-09-29
    audit, S2).
    """
    origin = request.headers.get("origin", "").strip().rstrip("/").lower()
    if origin and origin != "null" and origin != _SITE_ORIGIN:
        return True
    site = request.headers.get("sec-fetch-site", "").strip().lower()
    return bool(site) and site not in ("same-origin", "same-site", "none")


pool: AsyncConnectionPool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    if not DSN:
        logger.error("no NEWSLETTER_PG_DSN or LANGGRAPH_PG_DSN; signups will be refused")
        yield
        return
    pool = AsyncConnectionPool(DSN, min_size=1, max_size=4, kwargs={"autocommit": True}, open=False)
    await pool.open(wait=True)
    async with pool.connection() as conn:
        await conn.execute(_SCHEMA)
    logger.info("newsletter: ready on :%s", PORT)
    try:
        yield
    finally:
        await pool.close()


app = FastAPI(title="perseiagent.ai newsletter", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/health")
async def health():
    if pool is None:
        return JSONResponse({"ok": False, "detail": "no database"}, status_code=503)
    try:
        async with pool.connection() as conn:
            await conn.execute("SELECT 1")
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "detail": type(e).__name__}, status_code=503)
    return {"ok": True, "service": "newsletter"}


# The app downloads. Each file name carries its version, so a link on a
# static page would go stale with the next release; these ask GitHub which
# full release is latest and send the browser to that release's file.
# Cached so a busy day costs GitHub one call in ten minutes, and they fall
# back to the release page rather than failing. Linux since 2026-10-04: the
# AppImage (it updates itself) and the .deb.
RELEASES_API = "https://api.github.com/repos/DJG3DK/persei/releases/latest"
RELEASES_PAGE = "https://github.com/DJG3DK/persei/releases/latest"
_ASSETS = {
    "windows": re.compile(r"^(?:Persei|Tektonix)_[0-9A-Za-z.\-]+_x64-setup\.exe$"),
    "linux": re.compile(r"^(?:Persei|Tektonix)_[0-9A-Za-z.\-]+_amd64\.AppImage$"),
    "linux-deb": re.compile(r"^(?:Persei|Tektonix)_[0-9A-Za-z.\-]+_amd64\.deb$"),
}
_RELEASE_DOWNLOADS = "https://github.com/DJG3DK/persei/releases/download/"
_DOWNLOAD_TTL_S = 600
_download_cache: dict[str, tuple[float, str]] = {}


async def _latest_asset(kind: str) -> str | None:
    now = time.monotonic()
    hit = _download_cache.get(kind)
    if hit and now - hit[0] < _DOWNLOAD_TTL_S:
        return hit[1]
    try:
        async with httpx.AsyncClient(timeout=8, headers={"Accept": "application/vnd.github+json"}) as client:
            r = await client.get(RELEASES_API)
            r.raise_for_status()
            release = r.json()
    except Exception as e:  # noqa: BLE001 -- the release page is a fine answer too
        logger.warning("download: could not read the latest release: %s", type(e).__name__)
        return None
    for asset in release.get("assets") or []:
        url = asset.get("browser_download_url") or ""
        if _ASSETS[kind].match(asset.get("name") or "") and url.startswith(_RELEASE_DOWNLOADS):
            _download_cache[kind] = (now, url)
            return url
    return None


def reset_download_cache() -> None:
    _download_cache.clear()


@app.get("/download/windows")
async def download_windows():
    return RedirectResponse(await _latest_asset("windows") or RELEASES_PAGE, status_code=302)


@app.get("/download/linux")
async def download_linux():
    return RedirectResponse(await _latest_asset("linux") or RELEASES_PAGE, status_code=302)


@app.get("/download/linux-deb")
async def download_linux_deb():
    return RedirectResponse(await _latest_asset("linux-deb") or RELEASES_PAGE, status_code=302)


@app.post("/subscribe")
async def subscribe(request: Request, name: str = Form(""), email: str = Form(""),
                    website: str = Form("", alias=HONEYPOT_FIELD)):
    """Take one signup. Always answers with a redirect, never a bare status."""
    if not _subscribe_allowed(_client_ip(request)):
        logger.info("newsletter: refused a signup (rate limit)")
        return RedirectResponse(FAIL_URL, status_code=303)
    if _from_another_site(request):
        logger.info("newsletter: refused a signup (posted from another site)")
        return RedirectResponse(FAIL_URL, status_code=303)
    if website.strip():
        # Told "success" and stored nowhere: a refusal is a signal to adapt
        # to, and there is no person at the other end to mislead.
        logger.info("newsletter: dropped a signup (honeypot filled)")
        return RedirectResponse(OK_URL, status_code=303)

    name = " ".join(name.split())[:120]
    email = email.strip().lower()[:254]

    if not name or not _EMAIL.match(email):
        # No address in the log line: this is the one place a bad address
        # would otherwise be written down verbatim.
        logger.info("newsletter: rejected a signup (name=%s, address invalid=%s)",
                    bool(name), not _EMAIL.match(email))
        return RedirectResponse(FAIL_URL, status_code=303)

    if pool is None:
        logger.error("newsletter: no database, dropping a signup")
        return RedirectResponse(FAIL_URL, status_code=303)

    try:
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO newsletter_subscribers (email, name, unsubscribe_token, source) "
                "VALUES (%s, %s, %s, %s) "
                # Already on the list is not a failure -- see the module
                # docstring -- and it changes nothing: this used to refresh
                # the name and clear unsubscribed_at, on the theory that
                # signing up again was a statement of intent. Without a
                # confirmation mail it is only a statement that somebody
                # typed the address.
                "ON CONFLICT (email) DO NOTHING",
                (email, name, secrets.token_urlsafe(24), "landing-page"))
    except psycopg.Error:
        logger.exception("newsletter: could not store a signup")
        return RedirectResponse(FAIL_URL, status_code=303)

    logger.info("newsletter: signup stored (now %s on the list)", await _count())
    return RedirectResponse(OK_URL, status_code=303)


async def _count() -> int:
    if pool is None:
        return 0
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) AS n FROM newsletter_subscribers WHERE unsubscribed_at IS NULL")
            row = await cur.fetchone()
        return int(row[0]) if row else 0
    except psycopg.Error:
        return 0


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT)
