# Newsletter signup

The form at the bottom of [the landing page](../src/LandingPage.tsx) posts
here. It writes a name and an address down and does nothing else — **nothing
sends yet**.

It also stands in front of the app downloads (since 2026-10-21). The site's
download buttons go to `/get/<windows|linux|linux-deb>`: a short form (name,
email, an unticked "send me updates"), then a page that starts the download
and shows Linux the one-line installer. Each registration is a row in
`download_registrations`; ticking the box also adds the address to the
newsletter. A signed cookie (`DOWNLOAD_COOKIE_SECRET` in `.env`) lets a
returning visitor skip the form, and `/download/<kind>` without it goes to the
form. It is not a lock — the repository's release page is public — and the
apps' updates and the one-liner go to GitHub directly. nginx routes
`perseiagent.ai/get/` here.

Not part of Persei, for the same reason the landing page is not: a
self-hosted agent has no business shipping an endpoint that writes to
perseiagent.ai's mailing list. `site/` is dropped from the release tarball and
this goes with it.

## Shape

Everything is decided by one constraint: **the landing page ships no
JavaScript.** So the form is a plain HTML POST, which means

* form-encoded input rather than JSON;
* a `303` to a real page rather than a status code, because a browser would
  show a bare status to nobody — `/subscribed/` or `/subscribe-failed/`, both
  static pages in `site/public/`;
* same origin, so no CORS and no preflight. nginx routes
  `perseiagent.ai/newsletter/subscribe` here.

A repeat signup is a **success**. The person meant "put me on the list", they
are on the list, and an error would send them away thinking it had failed. It
changes nothing, though: there is no double opt-in, so a signup is not proof
it came from the address's owner, and it must not rename somebody or re-add
somebody who unsubscribed.

## Running it

```bash
cd site/server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # then put the database URL in it
pm2 start ecosystem.config.cjs && pm2 save
curl -s 127.0.0.1:8300/health
```

`.cjs`, not `.js`: `site/package.json` declares `"type": "module"`, which would
make pm2 load a `.js` config as ESM and die on `module.exports`.

## The data

One table, `newsletter_subscribers`, in whatever database `NEWSLETTER_PG_DSN`
names — by default the one the agent already uses, because that is what
`pg_dump` already backs up (`docs/backup.md`).

| column | why |
|---|---|
| `email`, `name` | what the form collects. `email` is unique, so a repeat is a no-op |
| `unsubscribe_token` | every issue needs an unsubscribe link; minted now so the first send does not have to backfill one for everybody |
| `confirmed_at` | null until there is a double opt-in, which needs a mail to confirm *with*. The column exists so adding it later is a write rather than a migration |
| `unsubscribed_at` | set instead of deleting the row, so a later signup does not silently re-add somebody who left |

Addresses are never written to the log. A log line is the easiest way for a
mailing list to leak, and it would leak into a file people tail.

## When sending starts

The list is the input; the changelog is the content. Nothing here sends, and
nothing should until there is an unsubscribe link in the template that
resolves against `unsubscribe_token`.
