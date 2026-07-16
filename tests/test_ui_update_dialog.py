from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from pi_manager.ui import InstallPiDialog


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_install_dialog_returns_to_manager_after_success(qapp, monkeypatch):
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda *_args, **_kwargs: pytest.fail(
            "successful update should not wait on another modal message box"
        ),
    )
    dialog = InstallPiDialog()

    dialog._done((0, "updated", ""))

    assert dialog.install_succeeded is True
    assert dialog.result() == QDialog.Accepted
    assert "正在返回管理器面板" in dialog.log.toPlainText()


def test_install_dialog_stays_open_after_failure(qapp, monkeypatch):
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args, **_kwargs: QMessageBox.Ok)
    dialog = InstallPiDialog()

    dialog._done((1, "", "npm failed"))

    assert dialog.install_succeeded is False
    assert dialog.result() != QDialog.Accepted
    assert dialog.btn_install.isEnabled() is True
