# agent-review

The review dashboard and merge control for [Persei](https://github.com/DJG3DK/persei).

A small Express service that shows what each project's agent workspace has that the live checkout
doesn't, surfaces the [commit-reviewer](../commit-reviewer)'s verdict, and —
if that verdict is `READY` — fast-forwards the work into live and pushes it.

## Endpoints

| route | does |
|---|---|
| `GET /api/projects` | the configured projects |
| `GET /api/projects/:name/status` | what the agent branch has that live doesn't, and whether it fast-forwards |
| `GET /api/projects/:name/diff` | the full diff, three-dot from the merge base |
| `POST /api/projects/:name/merge` | fast-forward into live, then push to `origin` |
| `POST /api/projects/:name/restart` | run the project's build steps, then restart its pm2 apps. Optional `preflight` URLs (see the example config) are checked first; a failure there is stage `preflight` and no build step runs |
| `GET /api/review/status`, `POST /api/review/check/:name` | reviewer state and a manual trigger |
| `GET /api/router/stats`, `GET /api/router/balance` | model-router usage and OpenRouter balance |

Every route except `GET /health` requires `X-Review-Secret` — the reads as
well as the writes, since 2026-09-23. In the container bundle this service is
on the compose network, not loopback, and the diff route is every project's
full source change. The console reaches it through `/_review/` (nginx on a host
install, the agent's own proxy in the bundle), and both inject the secret, so
the dashboard is unaffected. A hand-written nginx location that predates this
must carry the `X-Review-Secret` line from [INSTALL.md](../../INSTALL.md).

## Merging

The merge is gated: it refuses if the project has not been reviewed, if the reviewer's verdict is
stale relative to the current tip, or if the verdict is `NEEDS_FIXES`. `?force` overrides, which is
the operator's call to make.

It merges **exactly what was reviewed** — the branch recorded on the reviewer's verdict, not
whatever the newest branch happens to be. That is also why a stale ref is checked before use: review
state outlives the branch it names, and a verdict recorded against a branch that no longer exists
used to 500 this endpoint rather than being ignored.

The push to `origin` is deliberately **best-effort**: a push failure must not turn a successful
merge-and-deploy into an error, because the merge has already happened and is not being rolled back.
The result is reported instead. This was added after two agent commits merged and fully deployed
while GitHub sat one and two commits behind, with nothing anywhere reporting the drift.

## Layout

```
server.js              the service
public/index.html      the dashboard
ecosystem.config.js    pm2
```

Runs on `127.0.0.1:4100` behind nginx on a host install; in the container bundle it binds
`0.0.0.0` on the compose network, reached through the agent's own `/_review/` proxy.
