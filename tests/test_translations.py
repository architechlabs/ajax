"""Translation file consistency tests."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_DIR = ROOT / "custom_components" / "ajax"


def _leaf_paths(value: Any, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    """Return all scalar JSON paths from a nested translation object."""
    if isinstance(value, dict):
        paths: set[tuple[str, ...]] = set()
        for key, child in value.items():
            paths.update(_leaf_paths(child, (*prefix, key)))
        return paths
    return {prefix}


def test_translation_files_match_strings_schema() -> None:
    """Every shipped translation must expose the same leaf keys as strings.json."""
    strings = json.loads((INTEGRATION_DIR / "strings.json").read_text())
    expected_paths = _leaf_paths(strings)

    assert ("config", "step", "reconfigure", "data", "verify_ssl") in expected_paths
    assert ("config", "step", "reconfigure", "data_description", "verify_ssl") in expected_paths

    for translation_path in sorted((INTEGRATION_DIR / "translations").glob("*.json")):
        translation = json.loads(translation_path.read_text())
        assert _leaf_paths(translation) == expected_paths, translation_path.name


# ---------------------------------------------------------------------------
# ENUM options ⇄ translation parity
#
# Regression (code-review): several ENUM sensors / selects had their
# ``options`` list changed without updating the matching ``state`` block in
# strings.json (WaterStop motor_state / external_power, VideoEdge storage),
# so HA showed the raw key. Any literal ``options`` declared next to a
# ``translation_key`` MUST have every key translated.
# ---------------------------------------------------------------------------

_ENUM_BLOCK_RE = re.compile(
    r'"translation_key"\s*:\s*"(?P<tkey>[^"]+)"[^{}]*?"options"\s*:\s*\[(?P<opts>[^\]]*)\]'
    r'|"options"\s*:\s*\[(?P<opts2>[^\]]*)\][^{}]*?"translation_key"\s*:\s*"(?P<tkey2>[^"]+)"',
    re.S,
)


def _state_keys_by_translation_key() -> dict[str, set[str]]:
    strings = json.loads((INTEGRATION_DIR / "strings.json").read_text())
    out: dict[str, set[str]] = {}
    for platform in strings.get("entity", {}).values():
        for tkey, spec in platform.items():
            state = spec.get("state")
            if isinstance(state, dict):
                out[tkey] = set(state.keys())
    return out


def test_enum_options_have_matching_translations() -> None:
    """Every literal ``options`` list must be fully translated in strings.json."""
    state_keys = _state_keys_by_translation_key()
    failures: list[str] = []
    seen = 0

    for source in (INTEGRATION_DIR / "devices").glob("*.py"):
        text = source.read_text()
        for match in _ENUM_BLOCK_RE.finditer(text):
            tkey = match.group("tkey") or match.group("tkey2")
            opts_raw = match.group("opts") or match.group("opts2") or ""
            options = set(re.findall(r'"([^"]+)"', opts_raw))
            if not options:
                continue
            seen += 1
            translated = state_keys.get(tkey)
            if translated is None:
                failures.append(f"{source.name}: '{tkey}' has options {sorted(options)} but no state block")
                continue
            missing = options - translated
            if missing:
                failures.append(f"{source.name}: '{tkey}' options missing translation {sorted(missing)}")

    assert seen, "no literal-options ENUM specs found — regex likely broke"
    assert not failures, "untranslated ENUM options:\n" + "\n".join(failures)
