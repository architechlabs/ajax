"""Pin the runtime_data contract.

The integration migrated off `hass.data[DOMAIN]` to the modern
`entry.runtime_data` pattern (required for Quality Scale Gold). This
test catches accidental regressions where a contributor reintroduces
`hass.data[DOMAIN]` — silently working but flagged as Bronze-grade by
hassfest/QS reviewers.
"""

from __future__ import annotations

import re
from pathlib import Path

INTEGRATION_DIR = Path(__file__).parent.parent / "custom_components" / "ajax"


def test_no_hass_data_domain_usage() -> None:
    """No source file may read or write `hass.data[DOMAIN]`."""
    pattern = re.compile(r"hass\.data\s*(?:\[\s*DOMAIN\s*\]|\.(?:get|setdefault)\s*\(\s*DOMAIN)")
    offenders: list[str] = []
    for path in INTEGRATION_DIR.rglob("*.py"):
        text = path.read_text()
        for line_no, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{line_no}: {line.strip()}")
    assert not offenders, "Use entry.runtime_data, not hass.data[DOMAIN]:\n" + "\n".join(offenders)
