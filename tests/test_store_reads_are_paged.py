"""A bare `asearch(ns, limit=N)` is a page, not a namespace. Three audit
findings (B35, B38 and the project removal that deleted what it never
archived) were that one call read as "everything". agent/store_paging.py is
the one place allowed to make it; everyone else asks it for all_items or
recent_items.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWED = {ROOT / "agent" / "store_paging.py"}


def _capped_reads(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(), str(path))
    return [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        # asearch is the store's; a ranked `index.search(..., limit=)` asks
        # for its top N on purpose and is not a page of anything.
        and node.func.attr == "asearch"
        and any(kw.arg == "limit" for kw in node.keywords)
    ]


def test_no_capped_store_read_outside_store_paging():
    offenders = [
        f"{path.relative_to(ROOT)}:{line}"
        for path in sorted((ROOT / "agent").rglob("*.py"))
        if path not in ALLOWED and ".claude" not in path.parts
        for line in _capped_reads(path)
    ]
    assert not offenders, (
        "store reads with limit= outside agent/store_paging.py "
        "(use all_items or recent_items): " + ", ".join(offenders))
