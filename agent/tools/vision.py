"""Shared vision-description call, pinned to agent-vision -- used by both
describe_image (agent_tools.py, describes an uploaded/repo image file) and
browse_page (planning_tools.py, describes a screenshot captured live via
Playwright). Routed through ChatOpenAI (not a raw HTTP POST) so every call
participates in LangSmith tracing and shows up in the Analytics
model-usage-by-role scan under the "vision" role -- see
_classify_model_usage_role in agent/routers/analytics.py.
"""

import os

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

DEFAULT_DESCRIBE_PROMPT = (
    "Describe this image thoroughly for a developer who cannot see it: layout, all visible text "
    "verbatim, colors, any error messages, and anything unusual."
)


async def describe_image_bytes(image_bytes: bytes, mime: str, prompt: str | None = None) -> str:
    import base64

    b64 = base64.b64encode(image_bytes).decode()
    # NO client-side max_tokens, deliberately (2026-08-28): langchain-openai
    # converts any max_tokens into the newer `max_completion_tokens` wire
    # param (even one smuggled via model_kwargs -- it intercepts and converts
    # that too), and with require_parameters OpenRouter then demands a
    # provider supporting that exact param for the vision model: none do, so
    # EVERY describe_image call 404'd ("No endpoints found that can handle
    # the requested parameters"). The 1500-token cap lives on the router's
    # agent-vision deployment (the deployment's params.max_tokens) instead -- enforced
    # server-side, invisible to provider routing.
    model = ChatOpenAI(
        model="agent-vision",
        base_url=os.environ.get("MODEL_ROUTER_URL", "http://127.0.0.1:4001/v1"),
        api_key=SecretStr(os.environ.get("MODEL_ROUTER_KEY", os.environ.get("OPENAI_API_KEY", ""))),
        timeout=120,
    )
    response = await model.ainvoke([
        HumanMessage(content=[
            {"type": "text", "text": (prompt or "").strip() or DEFAULT_DESCRIBE_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]),
    ])
    return str(response.content)
