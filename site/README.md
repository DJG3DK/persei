# perseiagent.ai — the public landing page

Separate from the console on purpose. This is what a visitor sees; the console
is its own build at [`../frontend`](../frontend) on its own host
(`agent.perseiagent.ai`). They were one document until 2026-09-17, which is why
the product's marketing copy used to sit in front of a password box and ship
with every installation of the agent.

Two things follow from the split:

* **It is not part of Persei.** `scripts/package_release.sh` deletes `site/`
  from the tarball, and `install.sh` never looks at it. Somebody self-hosting
  the agent gets the console; they have no use for a page selling it to them.
* **It ships no JavaScript.** The controls are links and a plain HTML form, and
  both are HTML. `npm run build` renders the page to static markup and then
  strips the module script — see `scripts/render.mjs`, which fails the build if
  a script tag ever survives.

## The newsletter

The form at the bottom of the page posts to [`server/`](server/README.md), a
small service that writes a name and an address down. **Nothing sends yet.**
It is a plain HTML POST for the same reason everything else here is static:
there is no JavaScript to run a fetch, so the server answers with a redirect
to a page rather than a status code nobody would see.

## Build and deploy

```bash
cd site
npm ci
npm run build          # -> dist/  (HTML, CSS, images; no JS)
sudo ./deploy.sh       # copies dist/ to the nginx root and reloads
```

`deploy.sh` is a convenience for this deployment. The output is plain static
files, so anywhere that serves a directory will do — a CDN, an object store, a
different box entirely. Nothing here talks to the agent.

## Where things are

```
index.html        the shell: title, description, Open Graph, structured data
src/
  main.tsx        the build entry -- renders nothing, see its comment
  entry.tsx       the SSR entry scripts/render.mjs builds
  LandingPage.tsx the page
  LandingPage.css its styles
  tokens.css      this page's own copy of the Drafting palette
  assets/         the logo and the seven screenshots
public/           favicons, og-preview.png, robots.txt, sitemap.xml
scripts/render.mjs  renders to static HTML, strips the JavaScript
```

## The palette is a copy, deliberately

`src/tokens.css` duplicates the console's Drafting values rather than importing
`frontend/src/theme.css`. The console carries five user-selectable schemes; a
visitor has no account and therefore no preference, and the brand should not
depend on whoever signed in last. The copy is also what lets this directory be
lifted out and hosted anywhere. `tests/test_themes.py` checks the two agree on
the brand colours so the copy cannot drift unnoticed.
