"""Extract the human-readable text from a LangChain message's `content`.

`content` is not always a string. Depending on which provider the router is
proxying and whether the response streamed, an AIMessage's content can be a
LIST of typed blocks -- and Kimi K3 via OpenRouter routinely returns e.g.

    [{'type': 'text', 'text': 'Let me read the file.', 'index': 0},
     {'type': 'tool_call', 'id': 'chatcmpl-tool-...', 'name': '...', ...}]

Both live-view translators (agent/nodes/work.py and agent/planning_chat.py)
rendered `str(msg.content)` into the dashboard. For string content that was
fine; for block-list content the operator saw the raw Python dict repr --
and for a turn whose only block was the tool_call (21 of 76 messages in a
real planning session, 2026-08-27), the repr was ALL they saw. In the UI
that read as "tool failures / calls with no output", when the transcript
was actually healthy: every tool call in that session succeeded, the raw
repr just looked like debris.

Only `type: "text"` blocks carry operator-facing prose. tool_call blocks
are duplicates of `msg.tool_calls` (already rendered as "calling: ..."),
and thinking/reasoning blocks are model-internal.
"""


def content_text(content: object) -> str:
    """The joined text of a message's content -- '' if it has none."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return str(content)
