"""Persei's own model router.

Every deployment is `openrouter/...`, so the off-the-shelf proxy this
replaced was a proxy in front of a proxy, and its headline feature --
normalising many providers behind one OpenAI-compatible API -- is a job
OpenRouter already does. What we actually used it for is alias resolution,
ordered fallbacks, a billed-cost figure and a callback that writes our own
ledger. That is this package.

What it buys that the proxy could not:

  * HOT RELOAD. The old proxy read config.yaml once at startup, so repinning a model
    from the dashboard required restarting the router -- which kills every
    model call in flight, on every service sharing it. docs/architecture.md
    carries the warning: "never restart this while a task is mid-call".
  * PER-ALIAS TIMEOUTS. Exactly one deployment had a timeout; the rest had
    none, which is how a coder call sat upstream for 1802 seconds and returned
    280 tokens.
  * No framework pin held hostage by a dependency's use of private APIs.

It reads the SAME services/model-router/config.yaml, deliberately: the operator's
pins live there, the dashboard's Models page writes there, and a migration
would be a second thing to get wrong on the day of a cutover.
"""
