"""Regression tests for the hardened direct-only security model."""

from __future__ import annotations

from pathlib import Path


def test_no_retired_transport_modules_are_shipped() -> None:
    root = Path(__file__).parents[1] / "custom_components" / "ajax"
    for name in ("sqs_client.py", "sqs_manager.py", "sse_client.py", "sse_manager.py"):
        assert not (root / name).exists()


def test_no_third_party_cloud_transport_references() -> None:
    root = Path(__file__).parents[1] / "custom_components" / "ajax"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in ("aiobotocore", "boto3", "sse_client", "sse_manager", "sqs_client", "sqs_manager"):
            if needle in text:
                offenders.append(f"{path.relative_to(root)}:{needle}")
    assert not offenders, offenders


def test_ajax_api_base_url_is_fixed_and_redirects_are_disabled() -> None:
    root = Path(__file__).parents[1] / "custom_components" / "ajax"
    base = (root / "const.py").read_text(encoding="utf-8")
    transport = (root / "api" / "_base.py").read_text(encoding="utf-8")
    assert 'AJAX_REST_API_BASE_URL = "https://api.ajax.systems/api"' in base
    assert "allow_redirects=False" in transport
    assert "trust_env=False" in transport


def test_no_configurable_proxy_or_aws_settings() -> None:
    const = (Path(__file__).parents[1] / "custom_components" / "ajax" / "const.py").read_text(encoding="utf-8")
    assert "CONF_PROXY_URL" not in const
    assert "CONF_AWS_ACCESS_KEY_ID" not in const
    assert "CONF_AWS_SECRET_ACCESS_KEY" not in const
    assert "CONF_VERIFY_SSL" not in const
