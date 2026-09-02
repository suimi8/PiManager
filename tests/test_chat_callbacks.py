from __future__ import annotations

import inspect

from pi_manager.presentation import app, shell, workers
from pi_manager.ui import MainWindow, NAV_PAGES, Worker, drain_pending_connections, run_app


def test_ui_facade_reexports_presentation_symbols():
    assert MainWindow is shell.MainWindow
    assert NAV_PAGES is app.NAV_PAGES
    assert run_app is app.run_app
    assert drain_pending_connections is app.drain_pending_connections
    assert Worker is workers.Worker


def test_basic_and_enhanced_chat_callbacks_have_distinct_contracts():
    assert not hasattr(MainWindow, "_on_chat_done")
    assert list(inspect.signature(MainWindow._on_basic_chat_done).parameters) == [
        "self",
        "result",
    ]
    assert list(inspect.signature(MainWindow._on_enhanced_chat_done).parameters) == [
        "self",
        "result",
        "user_prompt",
    ]
