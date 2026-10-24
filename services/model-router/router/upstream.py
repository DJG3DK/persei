"""Talking to OpenRouter.

Two paths, because the agent uses both: a buffered call for anything carrying
tools (deep_agent sets disable_streaming="tool_calling", after measuring that
re-merging tool-call chunks cost ~25 CPU-seconds per call), and a streamed one
for plain text. Embeddings take the buffered path against a second URL and
differ in nothing else.

Both must end up with the same ledger line, which is the whole difficulty of
the streaming path: the usage block arrives in the LAST chunk, after the body
has already been handed to the client. So the stream is passed through
untouched and the usage is scraped from the chunks on the way past.

`usage: {"include": true}` is added to every request. That is what makes
OpenRouter report `usage.cost` -- the real billed figure -- rather than us
multiplying tokens by a rate table that disagrees with the invoice.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from collections.abc import AsyncIterator

import httpx

logger = logging.getLogger("model-router")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# The embedding path, added 2026-09-21. Same host, same key, same
# response envelope -- which is the only reason episode embeddings could
# be billed out of routing.jsonl rather than estimated from a rate table.
EMBEDDINGS_URL = "https://openrouter.ai/api/v1/embeddings"


@dataclass
class Usage:
    """What the ledger needs, however the response arrived."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    cost: float | None = None
    provider: str | None = None
    model: str | None = None

    @classmethod
    def from_payload(cls, payload: dict) -> Usage:
        usage = payload.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        return cls(
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            # A provider that says nothing about caching is not the same as one
            # reporting zero, but the ledger's readers treat absent as 0 and
            # the distinction has never been actionable.
            cached_tokens=details.get("cached_tokens") if isinstance(details, dict) else None,
            cost=usage.get("cost"),
            provider=payload.get("provider"),
            model=payload.get("model"),
        )

    def merge(self, other: Usage) -> None:
        """Later chunks win, but only where they actually say something."""
        for f in ("prompt_tokens", "completion_tokens", "cached_tokens", "cost", "provider", "model"):
            v = getattr(other, f)
            if v is not None:
                setattr(self, f, v)


# Status codes worth trying again on the SAME deployment. A 429 is the
# provider asking us to wait, not a reason to abandon the model the operator
# pinned -- config.yaml records two 429s a minute apart taking a whole demo
# down. 5xx is the provider failing transiently. Everything else (400 a
# malformed request, 401 a bad key, 404 an unknown model) will fail identically
# on a second attempt, so retrying it only adds latency to a certain failure.
TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def is_transient(status: int | None, error: str | None) -> bool:
    if status is not None:
        return status in TRANSIENT_STATUS
    # No status means the request never completed: a timeout, a dropped
    # connection, a DNS blip. All worth one more try.
    return bool(error)


@dataclass
class Attempt:
    """One try against one deployment."""

    alias: str
    model: str
    ok: bool
    status: int | None = None
    duration_s: float = 0.0
    error: str | None = None
    payload: dict | None = None
    usage: Usage = field(default_factory=Usage)


DEFAULT_PROVIDER_SORT = "throughput"
# An answer longer than this is a model that has lost the thread, not one
# with more to say: DeepSeek ran to the provider's 131,072-token ceiling on
# 2-3% of calls on some hosts (2026-09-24), seven-plus minutes each, while the
# agent's real answers stayed under ~15k. A deployment's own `max_tokens` or
# a caller's wins -- the agent sends 16k on its own seats since 2026-09-25
# (agent/model_client.py SEAT_MAX_TOKENS), so this reaches only callers that
# send no cap; embeddings carry no `messages` and are left alone.
DEFAULT_MAX_OUTPUT_TOKENS = 32768


def with_privacy(extra_body: dict, mode: str) -> dict:
    """extra_body with the router-wide privacy rule in its provider block.

    OpenRouter publishes no per-provider "trains on your data" flag, so the
    rule is enforced where it can be: on every request, by OpenRouter's own
    filter. A deployment that sets `zdr` or `data_collection` itself keeps
    its own choice."""
    provider = extra_body.get("provider") if isinstance(extra_body.get("provider"), dict) else {}
    if mode == "off" or "zdr" in provider or "data_collection" in provider:
        return extra_body
    rule = {"zdr": True} if mode == "zdr" else {"data_collection": "deny"}
    return {**extra_body, "provider": {**provider, **rule}}


def build_body(body: dict, model: str, extra_body: dict) -> dict:
    """The request as OpenRouter will see it.

    Deployment `extra_body` is merged UNDER the caller's body: a per-call
    argument (a one-off reasoning_effort, say) must win over a default set in
    config.yaml, or the config silently overrides the code.
    """
    out = {**extra_body, **body, "model": model}
    # Never forward our own routing metadata upstream.
    out.pop("metadata", None)
    # Speed over price when nothing else says how to pick a host: the stand-in
    # until router/fastest.py's own ranking arrives (see there for why).
    provider = out.get("provider") if isinstance(out.get("provider"), dict) else {}
    if not any(k in provider for k in ("order", "only", "sort")):
        out["provider"] = {"sort": DEFAULT_PROVIDER_SORT, **provider}
    if "messages" in out and "max_tokens" not in out and "max_completion_tokens" not in out:
        out["max_tokens"] = DEFAULT_MAX_OUTPUT_TOKENS
    usage = out.get("usage")
    out["usage"] = {**usage, "include": True} if isinstance(usage, dict) else {"include": True}
    if out.get("stream"):
        opts = out.get("stream_options")
        out["stream_options"] = {**opts, "include_usage": True} if isinstance(opts, dict) else {"include_usage": True}
    return out


async def call_once(client: httpx.AsyncClient, api_key: str, body: dict,
                    model: str, extra_body: dict, timeout_s: float) -> Attempt:
    """Buffered request. Used for tool-calling and any non-streaming call."""
    return await _post_once(client, OPENROUTER_URL, api_key, body, model, extra_body, timeout_s)


async def embed_once(client: httpx.AsyncClient, api_key: str, body: dict,
                     model: str, extra_body: dict, timeout_s: float) -> Attempt:
    """The same buffered request, against the embeddings endpoint.

    Deliberately the same body builder and the same Attempt, because the
    caller in app.py walks one fallback chain and writes one ledger line for
    both. An embedding whose failures or whose cost were shaped differently
    would be an embedding missing from the only spend figure anyone trusts.

    Measured against the live endpoint on 2026-09-21: it accepts the
    `usage: {"include": true}` build_body adds and answers with the billed
    `usage.cost` either way (1.4e-07 for 7 tokens on
    openai/text-embedding-3-small). It reports no completion tokens, so the
    ledger records null there rather than a zero it was never told.
    """
    return await _post_once(client, EMBEDDINGS_URL, api_key, body, model, extra_body, timeout_s)


async def _post_once(client: httpx.AsyncClient, url: str, api_key: str, body: dict,
                     model: str, extra_body: dict, timeout_s: float) -> Attempt:
    """One buffered POST, shared by every non-streaming path.

    Shared rather than copied: what counts as a failure here decides whether
    app.py retries the pinned deployment or falls through to a second one,
    and two copies of that judgement are two chances for one endpoint to
    quietly stop retrying a 429.
    """
    started = time.monotonic()
    payload = build_body(body, model, extra_body)
    try:
        r = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout_s,
        )
    except Exception as e:  # noqa: BLE001 -- a transport failure is a failed attempt, not a crash
        return Attempt(alias="", model=model, ok=False,
                       duration_s=time.monotonic() - started,
                       error=f"{type(e).__name__}: {str(e)[:300]}")

    took = time.monotonic() - started
    try:
        data = r.json()
    except ValueError:
        return Attempt(alias="", model=model, ok=False, status=r.status_code,
                       duration_s=took, error=f"non-JSON response: {r.text[:200]}")

    if r.status_code >= 400 or "error" in data:
        detail = data.get("error") if isinstance(data, dict) else None
        return Attempt(alias="", model=model, ok=False, status=r.status_code, duration_s=took,
                       error=json.dumps(detail)[:300] if detail else f"HTTP {r.status_code}",
                       payload=data)

    return Attempt(alias="", model=model, ok=True, status=r.status_code, duration_s=took,
                   payload=data, usage=Usage.from_payload(data))


async def stream_once(client: httpx.AsyncClient, api_key: str, body: dict, model: str,
                      extra_body: dict, timeout_s: float, usage_out: Usage) -> AsyncIterator[bytes]:
    """Streamed request, passed through verbatim.

    Chunks are forwarded as received -- no re-encoding, no re-chunking. The
    only thing done on the way past is reading `usage` out of whichever chunk
    carries it, into `usage_out`, so the caller can write the ledger line after
    the response has already been delivered.

    An error mid-stream cannot be retried onto a fallback: bytes have already
    reached the client, and a second attempt would splice two responses
    together. It is logged and the stream ends.
    """
    payload = build_body({**body, "stream": True}, model, extra_body)
    async with client.stream(
        "POST", OPENROUTER_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload, timeout=timeout_s,
    ) as r:
        if r.status_code >= 400:
            detail = (await r.aread())[:400]
            raise httpx.HTTPStatusError(f"HTTP {r.status_code}: {detail!r}", request=r.request, response=r)
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                blob = line[6:].strip()
                if blob and blob != "[DONE]":
                    try:
                        usage_out.merge(Usage.from_payload(json.loads(blob)))
                    except ValueError:
                        pass  # a chunk we cannot parse is still a chunk to forward
            yield (line + "\n").encode()
