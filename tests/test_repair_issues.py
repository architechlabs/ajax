"""Pin the Repairs translation_key contract.

Each `ir.async_create_issue(translation_key=...)` call in the runtime
must have a matching entry under `issues.<key>` in `strings.json`,
otherwise HA renders a bare key in the Repairs UI. This catches missing
translations *and* dangling repair issues raised with a typo in the key.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

INTEGRATION_DIR = Path(__file__).parent.parent / "custom_components" / "ajax"


def _runtime_translation_keys() -> set[str]:
    """Scan source files for `translation_key="..."` inside repair issue calls."""
    keys: set[str] = set()
    pattern = re.compile(
        r"ir\.async_create_issue\s*\([^)]*?translation_key\s*=\s*[\"']([\w_]+)[\"']",
        re.DOTALL,
    )
    for path in INTEGRATION_DIR.rglob("*.py"):
        keys.update(pattern.findall(path.read_text()))
    return keys


def test_every_repair_issue_has_a_strings_entry() -> None:
    strings = json.loads((INTEGRATION_DIR / "strings.json").read_text())
    declared = set(strings.get("issues", {}))
    used = _runtime_translation_keys()
    missing = used - declared
    assert not missing, f"Repair issues without a strings.json entry: {sorted(missing)}"


def test_known_repair_issues_are_actually_raised() -> None:
    """Every entry under `issues.` must correspond to a real repair call.

    Catches dead keys left over from features that were ripped out — a
    Repairs entry with no caller is just docs that lie.
    """
    strings = json.loads((INTEGRATION_DIR / "strings.json").read_text())
    declared = set(strings.get("issues", {}))
    used = _runtime_translation_keys()
    dead = declared - used
    assert not dead, f"strings.json issues with no caller: {sorted(dead)}"
