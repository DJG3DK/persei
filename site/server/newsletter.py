"""The newsletter signup behind perseiagent.ai.

Deliberately not part of the agent. It belongs to the landing page -- the same
reasoning that moved the page out of the product: a self-hosted Persei has
no business shipping an endpoint that writes to perseiagent.ai's mailing list.
`site/` is excluded from the release tarball, and this goes with it.

Two jobs: take a name and an address from the newsletter form on the landing
page and write them down (nothing sends yet, on purpose), and stand in front
of the app downloads, which since 2026-10-21 ask for a name and an address
first (see "Downloads" below).

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

import hashlib
import hmac
import html
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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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

-- One row per download registration, so a returning visitor on a new device
-- is a second row rather than an overwrite.
CREATE TABLE IF NOT EXISTS download_registrations (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    platform TEXT NOT NULL,
    wants_updates BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS download_registrations_email ON download_registrations (email);
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


# ── Downloads, behind a name and an address (2026-10-21) ──────────────────────
#
# The site's download buttons go to /get/<kind>: a short form (name, email,
# an unticked "send me updates"). Submitting it stores a row, sets a signed
# cookie and lands on a page that starts the download and, for Linux, shows
# the one-line installer. /download/<kind> -- the redirect to GitHub's file --
# asks for that cookie, so an old bookmark goes through the form too; a
# returning visitor's cookie skips it.
#
# Not a lock: the repository is public, and GitHub's release page hands the
# same files to anyone. This is for the people who come through the site,
# which is most of them. The apps' own updates and the one-line installer go
# to GitHub directly and never pass through here.

_KINDS = {
    "windows": "Windows",
    "linux": "Linux",
    "linux-deb": "Linux (.deb)",
}
COOKIE = "persei_dl"
_COOKIE_MAX_AGE = 365 * 24 * 3600
# Signs the cookie. From the environment so a restart keeps everyone's; a
# missing one is a random key for this process -- registrations still work,
# they are just asked again after the next restart.
_SECRET = (os.environ.get("DOWNLOAD_COOKIE_SECRET") or "").encode() or secrets.token_bytes(32)
if not os.environ.get("DOWNLOAD_COOKIE_SECRET"):
    logger.warning("download: no DOWNLOAD_COOKIE_SECRET; cookies last only until a restart")


def _sign(reg_id: int) -> str:
    body = f"{reg_id}.{int(time.time())}"
    mac = hmac.new(_SECRET, body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{mac}"


def _registered(request: Request) -> bool:
    value = request.cookies.get(COOKIE) or ""
    try:
        reg_id, issued, mac = value.split(".")
        int(reg_id), int(issued)
    except ValueError:
        return False
    want = hmac.new(_SECRET, f"{reg_id}.{issued}".encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(mac, want) and time.time() - int(issued) < _COOKIE_MAX_AGE


async def _download(request: Request, kind: str):
    if not _registered(request):
        return RedirectResponse(f"/get/{kind}", status_code=303)
    return RedirectResponse(await _latest_asset(kind) or RELEASES_PAGE, status_code=302)


@app.get("/download/windows")
async def download_windows(request: Request):
    return await _download(request, "windows")


@app.get("/download/linux")
async def download_linux(request: Request):
    return await _download(request, "linux")


@app.get("/download/linux-deb")
async def download_linux_deb(request: Request):
    return await _download(request, "linux-deb")


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
{head}<title>{title} — Persei</title>
<link rel="icon" href="/favicon.ico" sizes="any">
<style>
:root{{color-scheme:dark}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;box-sizing:border-box;
  background:#11151a;color:#edf0f4;font:16px/1.6 -apple-system,BlinkMacSystemFont,"Inter","Segoe UI",Roboto,sans-serif;
  background-image:radial-gradient(46% 38% at 12% 4%,rgba(201,162,39,.16),transparent 70%)}}
main{{width:100%;max-width:440px}}
.brand{{display:inline-flex;align-items:center;gap:8px;color:#edf0f4;text-decoration:none;font-weight:650;letter-spacing:.18em;font-size:14px;margin-bottom:28px}}
h1{{font-size:24px;line-height:1.25;margin:0 0 8px}}
p{{color:#a3adbb;margin:0 0 18px}}
label{{display:block;font-size:13px;font-weight:650;letter-spacing:.04em;text-transform:uppercase;color:#a3adbb;margin:16px 0 6px}}
input[type=text],input[type=email]{{width:100%;box-sizing:border-box;padding:11px 12px;border-radius:6px;border:1px solid #343c48;
  background:#0d1116;color:#edf0f4;font:inherit}}
input:focus{{outline:2px solid #c9a227;outline-offset:1px;border-color:#c9a227}}
.check{{display:flex;gap:10px;align-items:flex-start;text-transform:none;letter-spacing:0;font-weight:400;font-size:15px;color:#edf0f4;margin-top:18px}}
.check input{{margin-top:5px;accent-color:#c9a227}}
.hp{{position:absolute;left:-10000px;width:1px;height:1px;overflow:hidden}}
button,a.btn{{display:inline-block;margin-top:22px;padding:12px 22px;border-radius:6px;border:0;background:#c9a227;color:#14102e;
  font:inherit;font-weight:650;text-decoration:none;cursor:pointer}}
.small{{font-size:13px;color:#8a94a3;margin-top:22px}}
.small a,a.quiet{{color:#a3adbb}}
.err{{background:rgba(229,72,77,.12);border:1px solid rgba(229,72,77,.4);color:#f2b8ba;padding:10px 12px;border-radius:6px;font-size:14px}}
pre{{background:#0d1116;border:1px solid #343c48;border-radius:6px;padding:12px;font-size:13px;color:#edf0f4;white-space:pre-wrap;overflow-wrap:anywhere}}
ol{{color:#a3adbb;padding-left:20px}}
</style></head>
<body><main>
<a class="brand" href="/"><svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true"><rect x="3.8" y="3.8" width="16.4" height="16.4" rx="1.97" fill="#EFBF4E" transform="rotate(45 12 12)"/><path d="M9.44 7.71H12.45A3.02 3.02 0 0 1 12.45 13.74H11.4V16.76H9.44ZM11.4 9.52H12.38A1.21 1.21 0 0 1 12.38 11.93H11.4Z" fill="#0B1426" fill-rule="evenodd"/></svg>PERSEI</a>
{body}
</main></body></html>
"""

_PRIVACY = ("Your name and email are kept so I know who is using Persei. They are not shared or sold, "
            "and you only get email if you tick the box. To have them deleted, write to "
            '<a href="mailto:danny@perseiagent.ai">danny@perseiagent.ai</a>.')


def _page(title: str, body: str, head: str = "", status: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=html.escape(title), body=body, head=head), status_code=status,
                        headers={"Cache-Control": "no-store"})


def _form(kind: str, name: str = "", email: str = "", updates: bool = False, error: str = "",
          status: int = 200) -> HTMLResponse:
    label = _KINDS[kind]
    err = f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ""
    body = f"""<h1>Download Persei for {html.escape(label)}</h1>
<p>Tell me who you are and the download starts straight away.</p>
{err}
<form method="post" action="/get/{kind}">
<label for="name">Name</label>
<input id="name" name="name" type="text" autocomplete="name" required maxlength="120" value="{html.escape(name)}">
<label for="email">Email</label>
<input id="email" name="email" type="email" autocomplete="email" required maxlength="254" value="{html.escape(email)}">
<div class="hp" aria-hidden="true"><label for="{HONEYPOT_FIELD}">Website</label>
<input id="{HONEYPOT_FIELD}" name="{HONEYPOT_FIELD}" type="text" tabindex="-1" autocomplete="off"></div>
<label class="check"><input type="checkbox" name="updates" value="1"{" checked" if updates else ""}>
<span>Send me updates: new releases and what's coming, a few times a year.</span></label>
<button type="submit">Download</button>
</form>
<p class="small">{_PRIVACY}</p>"""
    return _page(f"Download for {label}", body, status=status)


def _ready(kind: str) -> HTMLResponse:
    """The page the download starts from. A meta refresh, not a script: the
    redirect it follows ends in a file, so the browser downloads it and stays
    on this page."""
    label = _KINDS[kind]
    head = f'<meta http-equiv="refresh" content="1;url=/download/{kind}">\n'
    if kind == "windows":
        extra = """<ol><li>Run the installer</li><li>Let it set up Docker if it asks</li>
<li>Enter your OpenRouter key and projects folder</li><li>Pick your password and sign in</li></ol>"""
    else:
        other = ('<a class="quiet" href="/download/linux-deb">the .deb for Debian / Ubuntu</a>' if kind == "linux"
                 else '<a class="quiet" href="/download/linux">the AppImage</a>')
        extra = f"""<p>Or install it with one line, which also adds it to your app menu:</p>
<pre><code>curl -fsSL {html.escape(SITE_URL)}/install-linux.sh | sh</code></pre>
<p>Prefer a package? Get {other}.</p>"""
    body = f"""<h1>Your download is starting</h1>
<p>Persei for {html.escape(label)}. If nothing happens, <a class="quiet" href="/download/{kind}">download it here</a>.</p>
{extra}
<a class="btn" href="/">Back to Persei</a>
<p class="small">Questions? <a href="mailto:danny@perseiagent.ai">danny@perseiagent.ai</a></p>"""
    return _page("Your download is starting", body, head=head)


@app.get("/get/{kind}")
async def get_form(request: Request, kind: str):
    if kind not in _KINDS:
        return RedirectResponse("/#install", status_code=303)
    return _ready(kind) if _registered(request) else _form(kind)


@app.post("/get/{kind}")
async def register(request: Request, kind: str, name: str = Form(""), email: str = Form(""),
                   updates: str = Form(""), website: str = Form("", alias=HONEYPOT_FIELD)):
    """Store a download registration, then start the download. Never a bare
    status: there is no script on the page to show one."""
    if kind not in _KINDS:
        return RedirectResponse("/#install", status_code=303)
    wants_updates = updates.strip() == "1"
    name = " ".join(name.split())[:120]
    email = email.strip().lower()[:254]
    if _from_another_site(request):
        logger.info("download: refused a registration (posted from another site)")
        return _form(kind, name, email, wants_updates, "That didn't come from this page. Please try again.", 400)
    if not _subscribe_allowed(_client_ip(request)):
        logger.info("download: refused a registration (rate limit)")
        return _form(kind, name, email, wants_updates, "Too many tries from here. Wait a minute and try again.", 429)
    if website.strip():
        logger.info("download: dropped a registration (honeypot filled)")
        return RedirectResponse("/", status_code=303)
    if not name or not _EMAIL.match(email):
        return _form(kind, name, email, wants_updates,
                     "Please enter your name and a real email address." if not name
                     else "That email address doesn't look right.", 400)
    if pool is None:
        logger.error("download: no database, cannot store a registration")
        return _form(kind, name, email, wants_updates, "Something went wrong on my side. Please try again shortly.", 503)
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO download_registrations (name, email, platform, wants_updates) "
                "VALUES (%s, %s, %s, %s) RETURNING id", (name, email, kind, wants_updates))
            reg_id = (await cur.fetchone())[0]
            if wants_updates:
                # The newsletter's own rules: an address already there keeps its
                # row, and one that unsubscribed stays unsubscribed.
                await conn.execute(
                    "INSERT INTO newsletter_subscribers (email, name, unsubscribe_token, source) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (email) DO NOTHING",
                    (email, name, secrets.token_urlsafe(24), "download"))
    except psycopg.Error:
        logger.exception("download: could not store a registration")
        return _form(kind, name, email, wants_updates, "Something went wrong on my side. Please try again shortly.", 503)
    logger.info("download: registration stored (%s, updates=%s)", kind, wants_updates)
    response = RedirectResponse(f"/get/{kind}", status_code=303)
    response.set_cookie(COOKIE, _sign(reg_id), max_age=_COOKIE_MAX_AGE, httponly=True,
                        secure=SITE_URL.startswith("https://"), samesite="lax", path="/")
    return response


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
