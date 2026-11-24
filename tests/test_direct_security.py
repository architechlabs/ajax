"""Regression tests for the hardened direct-only security model.

Real-time events come only from the Ajax-provided AWS SQS queue (direct
mode). Proxy mode and the SSE relay stay removed.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parents[1] / "custom_components" / "ajax"


def test_no_retired_transport_modules_are_shipped() -> None:
    for name in ("sse_client.py", "sse_manager.py"):
        assert not (ROOT / name).exists()


def test_sqs_transport_is_shipped() -> None:
    for name in ("sqs_client.py", "sqs_manager.py"):
        assert (ROOT / name).exists()
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert any(req.startswith("aiobotocore") for req in manifest["requirements"])


def test_no_proxy_or_sse_transport_references() -> None:
    offenders: list[str] = []
    for path in ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in ("sse_client", "sse_manager", "SSEManager", "AjaxSSEClient", "boto3"):
            if needle in text:
                offenders.append(f"{path.relative_to(ROOT)}:{needle}")
    assert not offenders, offenders


def test_ajax_api_base_url_is_fixed_and_redirects_are_disabled() -> None:
    base = (ROOT / "const.py").read_text(encoding="utf-8")
    transport = (ROOT / "api" / "_base.py").read_text(encoding="utf-8")
    assert 'AJAX_REST_API_BASE_URL = "https://api.ajax.systems/api"' in base
    assert "allow_redirects=False" in transport
    assert "trust_env=False" in transport


def test_no_configurable_proxy_settings() -> None:
    const = (ROOT / "const.py").read_text(encoding="utf-8")
    assert "CONF_PROXY_URL" not in const
    assert "CONF_VERIFY_SSL" not in const
    assert "AUTH_MODE_PROXY" not in const
