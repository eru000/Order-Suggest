"""Stable ASGI entrypoint.

The public ``back`` module remains a compatibility facade while HTTP domains are
incrementally moved out of ``api_compat``. Replacing the module object preserves
existing monkeypatch-based integrations and the ``uvicorn back:app`` contract.
"""

from __future__ import annotations

import sys

import api_compat as _api


def create_app():
    return _api.app


_api.create_app = create_app
sys.modules[__name__] = _api
