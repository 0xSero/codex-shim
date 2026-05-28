from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_cursor_passthrough_by_default(monkeypatch, request):
    if "cursor_present" in request.fixturenames:
        return

    def _off(**_kwargs):
        return False

    for target in (
        "codex_shim.cursor_passthrough.cursor_passthrough_available",
        "codex_shim.server.cursor_passthrough_available",
        "codex_shim.catalog.cursor_passthrough_available",
        "codex_shim.cli.cursor_passthrough_available",
    ):
        monkeypatch.setattr(target, _off, raising=False)
