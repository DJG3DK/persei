import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest


@pytest.fixture(autouse=True)
def _no_catalogue_fetch(monkeypatch):
    """The app looks up what each model's providers take before a call
    (router/fastest.py SupportedParams). Tests never reach OpenRouter: the
    app's copy passes bodies through unchanged, and test_supported_params.py
    drives the class itself with a fake catalogue."""
    from router import app as app_module

    class _PassThrough:
        async def body_for(self, client, api_key, model, extra_body, body):
            return body

    monkeypatch.setattr(app_module, "supported", _PassThrough())
