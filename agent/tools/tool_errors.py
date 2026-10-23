"""Turn an unexpected tool exception into a tool RESULT the model can read.

Why this exists at all: langgraph's ToolNode default error handler
(_default_handle_tool_errors, langgraph/prebuilt/tool_node.py) returns a
message ONLY for ToolInvocationError -- the "you called this tool with the
wrong arguments" case -- and RE-RAISES everything else. So an exception
escaping a tool body doesn't fail that one tool call, it tears down the whole
graph run: astream_events stops mid-turn, run_planning_turn's caller gets a
traceback, and there is nothing left alive for the model to retry with. A
recoverable mistake the model would have shrugged off in one turn kills the
entire session instead.

That is exactly how a planning session died on read_project_file(repo=...,
path=".git/HEAD"): every project sandbox is a git WORKTREE, and a worktree's
`.git` is a one-line pointer FILE, not a directory (agent/tools/sandbox.py's
mount code documents the same fact from the other side) -- so opening
`.git/HEAD` raises NotADirectoryError [Errno 20]. read_project_file caught
only FileNotFoundError, so it escaped and took the turn down with it.

This is the net UNDER each tool's own error handling, not a replacement for
it: a message from here is generic by definition, and a model recovers far
faster from "that path runs through a file" than from "OSError". Keep
catching what you can describe well; this catches what you didn't predict.

GraphBubbleUp is re-raised deliberately: GraphInterrupt (deep_agent.py's
INTERRUPT_ON approval gate) and ParentCommand are control flow, not
failures -- swallowing one would turn an approval prompt into a silent
"ERROR" string and wedge the gate forever. asyncio.CancelledError needs no
special case: it derives from BaseException, not Exception.
"""

import functools
import inspect
from collections.abc import Callable
from typing import Any, cast

from langgraph.errors import GraphBubbleUp


def _describe(name: str, e: Exception) -> str:
    # OSError's str() embeds the absolute host path ("[Errno 20] Not a
    # directory: '/home/agent-workspaces/a large project/.git/HEAD'"). agent/tools/
    # files.py deliberately refuses to name the sandbox root to the model --
    # it leaks infrastructure detail and contradicts the "/workspace IS the
    # repo root" story the agent is told everywhere else -- so use the errno
    # text alone and let the tool's own handlers name the repo-relative path.
    detail = e.strerror if isinstance(e, OSError) and e.strerror else str(e)
    return (
        f"ERROR: {name} failed unexpectedly ({type(e).__name__}: {detail}). "
        "This is a tool-level failure, not a refusal -- adjust the arguments and carry on with "
        "the task; repeating the identical call will hit the identical failure."
    )


def tool_errors_to_text[F: Callable[..., Any]](fn: F) -> F:
    """Wrap a tool's underlying function so no Exception escapes it.

    Apply BELOW @tool -- decorators run bottom-up, so @tool builds its schema
    from the wrapper:

        @tool
        @tool_errors_to_text
        def read_project_file(repo: str, path: str) -> str: ...

    functools.wraps copies __name__/__doc__/__annotations__ and sets
    __wrapped__, so @tool's schema inference (inspect.signature, which follows
    __wrapped__) still sees the original signature and docstring.
    """
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except GraphBubbleUp:
                raise
            except Exception as e:  # noqa: BLE001 -- the entire point: nothing escapes a tool
                return _describe(fn.__name__, e)
        # A tool returns text either way, so the wrapper stands in for fn.
        return cast(F, async_wrapper)

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except GraphBubbleUp:
            raise
        except Exception as e:  # noqa: BLE001
            return _describe(fn.__name__, e)
    return cast(F, wrapper)
