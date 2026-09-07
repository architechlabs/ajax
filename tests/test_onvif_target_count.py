"""Pin the ONVIF target_count vs connected_count contract.

NVRs are intentionally skipped from ONVIF event subscription (their
channel→camera mapping is unreliable). Reporting the repair issue with
``len(video_edges)`` as the denominator counted the NVR and showed
``connected=2/total=3`` to users with 2 cameras + 1 NVR — making them
think a camera was broken when nothing was wrong. ``target_count``
returns the post-NVR-filter count so the issue says ``2/2`` (all good)
and gets auto-deleted.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

# `onvif-zeep-async` is an optional runtime dependency; the test env
# doesn't ship it. Stub the package surface before importing the manager.
for name in ("onvif", "onvif.client", "onvif.exceptions", "zeep", "zeep.exceptions"):
    if name not in sys.modules:
        mod = ModuleType(name)
        if name == "onvif":
            mod.__file__ = "/dev/null/onvif/__init__.py"
            mod.ONVIFCamera = MagicMock()  # type: ignore[attr-defined]
        if name == "onvif.exceptions":
            mod.ONVIFError = type("ONVIFError", (Exception,), {})  # type: ignore[attr-defined]
        if name == "zeep.exceptions":
            mod.Fault = type("Fault", (Exception,), {})  # type: ignore[attr-defined]
            mod.TransportError = type("TransportError", (Exception,), {})  # type: ignore[attr-defined]
        sys.modules[name] = mod

from custom_components.ajax.onvif_manager import AjaxOnvifManager  # noqa: E402


def _make_manager(target_ids: set[str], connected_ids: set[str]) -> AjaxOnvifManager:
    """Build a manager modelling the real runtime state.

    ``target_ids`` is the set of non-NVR cameras the manager is trying to
    connect (``async_start`` records this regardless of success). Only the
    cameras that actually connected appear in ``_clients`` — a failed
    connection is dropped, never inserted, exactly as in production.
    """
    mgr = AjaxOnvifManager(username="u", password="p", event_callback=lambda _e: None)
    mgr._target_ids = set(target_ids)
    for client_id in connected_ids:
        mgr._clients[client_id] = SimpleNamespace(connected=True)  # type: ignore[assignment]
    return mgr


def test_target_count_returns_zero_when_no_targets() -> None:
    assert _make_manager(set(), set()).target_count == 0


def test_target_count_excludes_nvrs_by_design() -> None:
    """`async_start` never targets NVRs — so target_count = camera count.

    A user with 2 cameras + 1 NVR has 2 targets (the NVR is skipped), so
    target_count must be 2 — never 3.
    """
    mgr = _make_manager({"cam1", "cam2"}, {"cam1", "cam2"})
    assert mgr.target_count == 2
    assert mgr.connected_count == 2


def test_connected_count_under_target_means_partial() -> None:
    """A camera that fails to connect stays in _target_ids but not _clients.

    This is the real partial-failure state the repair issue must detect.
    """
    mgr = _make_manager({"cam1", "cam2", "cam3"}, {"cam1", "cam3"})
    assert mgr.target_count == 3
    assert mgr.connected_count == 2
