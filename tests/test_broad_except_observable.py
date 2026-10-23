"""Every broad ``except`` in agent/ either surfaces the error or says why it doesn't.

A bare ``except:``, ``except Exception`` or ``except BaseException`` (alone or
inside a tuple) that neither re-raises, nor logs, nor hands the caught
exception on is how a failure disappears without a trace. Each such handler
must do one of:

* ``raise`` anywhere in its body;
* log -- call ``.exception/.error/.warning/.warn/.info/.debug/.critical/.log``
  on ``logging`` or on a name (or attribute) that is ``logger``/``log`` or
  ends in ``logger``/``_log`` (``_logger``, ``self._log``, ``LOG``...), or call
  ``traceback.print_exc``;
* record the error: bind it (``except Exception as e``) and read ``e`` in the
  body -- return it, store it, put it in a result the caller sees;
* or carry an explicit marker on the ``except`` line:
  ``# noqa: BLE001 -- <why silence is right here>``. The reason is required:
  a bare ``# noqa: BLE001`` only quiets ruff and does not excuse a handler.

There is no allowlist file: the end state is zero unexplained handlers.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "agent"

_BROAD = {"Exception", "BaseException"}
_LOG_METHODS = {"exception", "error", "warning", "warn", "info", "debug", "critical", "log"}
_MARKER = re.compile(r"#\s*noqa:[^#]*\bBLE001\b[^#]*?\s--\s*\S")


def _sources() -> list[Path]:
    # relative parts: a worktree checkout itself lives under .claude/
    return [p for p in sorted(AGENT.rglob("*.py"))
            if not {".claude", "__pycache__"} & set(p.relative_to(ROOT).parts)]


def _is_broad(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    if t is None:
        return True
    names = t.elts if isinstance(t, ast.Tuple) else [t]
    return any(isinstance(n, ast.Name) and n.id in _BROAD for n in names)


def _is_logger(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute):
        name = node.attr
    elif isinstance(node, ast.Name):
        name = node.id
    else:
        return False
    name = name.lower()
    return name in {"logging", "logger", "log"} or name.endswith(("logger", "_log"))


def _observable(handler: ast.ExceptHandler) -> bool:
    for stmt in handler.body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Raise):
                return True
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                f = node.func
                if f.attr in _LOG_METHODS and _is_logger(f.value):
                    return True
                if f.attr == "print_exc" and isinstance(f.value, ast.Name) and f.value.id == "traceback":
                    return True
            if (handler.name and isinstance(node, ast.Name) and node.id == handler.name
                    and isinstance(node.ctx, ast.Load)):
                return True
    return False


def _violations() -> list[str]:
    out = []
    for path in _sources():
        src = path.read_text(encoding="utf-8")
        lines = src.splitlines()
        for node in ast.walk(ast.parse(src, filename=str(path))):
            if not isinstance(node, ast.ExceptHandler) or not _is_broad(node):
                continue
            if _MARKER.search(lines[node.lineno - 1]) or _observable(node):
                continue
            out.append(f"{path.relative_to(ROOT)}:{node.lineno}: {lines[node.lineno - 1].strip()}")
    return out


def test_broad_excepts_log_reraise_or_explain():
    bad = _violations()
    assert not bad, (
        "broad except that swallows the error -- log it, re-raise, narrow it, or mark the\n"
        "except line `# noqa: BLE001 -- <reason>`:\n  " + "\n  ".join(bad)
    )


def _swallows(code: str) -> bool:
    h = next(n for n in ast.walk(ast.parse(code)) if isinstance(n, ast.ExceptHandler))
    return _is_broad(h) and not _observable(h)


def test_detector_flags_silent_handlers():
    assert _swallows("try:\n x()\nexcept Exception:\n pass\n")
    assert _swallows("try:\n x()\nexcept:\n return None\n")
    assert _swallows("try:\n x()\nexcept (ValueError, BaseException) as e:\n return None\n")


def test_detector_passes_observable_handlers():
    assert not _swallows("try:\n x()\nexcept ValueError:\n pass\n")
    assert not _swallows("try:\n x()\nexcept Exception:\n logger.warning('x')\n")
    assert not _swallows("try:\n x()\nexcept Exception:\n self._log.exception('x')\n")
    assert not _swallows("try:\n x()\nexcept Exception as e:\n return {'error': str(e)}\n")
    assert not _swallows("try:\n x()\nexcept Exception:\n cleanup()\n raise\n")


def test_marker_shape():
    assert _MARKER.search("except Exception:  # noqa: BLE001 -- the loop outlives a bad tick")
    assert not _MARKER.search("except Exception:  # noqa: BLE001")
    assert not _MARKER.search("except Exception:  # noqa: BLE001 --")
