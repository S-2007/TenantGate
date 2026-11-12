"""The README's results tables must be byte-identical copies of docs/RESULTS.md (no drift).

Blocks are delimited by ``<!-- BEGIN:<name> -->`` / ``<!-- END:<name> -->`` markers in both files.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_BLOCK = re.compile(r"<!-- BEGIN:([a-z0-9_-]+) -->\n(.*?)<!-- END:\1 -->", re.DOTALL)


def _blocks(path: Path) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in _BLOCK.finditer(path.read_text(encoding="utf-8"))}


def test_readme_result_blocks_match_results_md():
    readme, results = _blocks(ROOT / "README.md"), _blocks(ROOT / "docs" / "RESULTS.md")
    assert readme, "README has no synced result blocks"
    for name, body in readme.items():
        assert name in results, f"block {name} missing from docs/RESULTS.md"
        assert body == results[name], f"README block {name} differs from docs/RESULTS.md"


def test_every_readme_number_line_comes_from_results_md():
    """Outside the synced blocks the README must not state measured numbers of its own."""
    text = _BLOCK.sub("", (ROOT / "README.md").read_text(encoding="utf-8"))
    measured = re.findall(r"\b\d+(?:\.\d+)?\s?(?:ms|%)\b", text)
    assert not measured, f"numbers outside synced blocks: {measured[:5]}"
