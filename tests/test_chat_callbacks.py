from __future__ import annotations

import inspect

from pi_manager.ui import MainWindow
from pi_manager.ui_features import FeatureMixin


def test_basic_and_enhanced_chat_callbacks_have_distinct_contracts():
    assert not hasattr(MainWindow, "_on_chat_done")
    assert list(inspect.signature(MainWindow._on_basic_chat_done).parameters) == [
        "self",
        "result",
    ]
    assert list(inspect.signature(FeatureMixin._on_enhanced_chat_done).parameters) == [
        "self",
        "result",
        "user_prompt",
    ]
