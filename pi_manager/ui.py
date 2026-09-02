"""Compatibility re-export of the desktop UI facade.

Behavior lives in ``presentation/``. This module keeps the historical import
surface used by tests, ``main.py``, and the Cursor helper path.
"""
from __future__ import annotations

from .presentation.dialogs import (
    FetchModelsDialog as FetchModelsDialog,
    InstallPiDialog as InstallPiDialog,
    ProviderEditorDialog as ProviderEditorDialog,
    ProviderKeysDialog as ProviderKeysDialog,
    SetupWizardDialog as SetupWizardDialog,
)
from .presentation.workers import (
    BatchTestWorker as BatchTestWorker,
    Worker as Worker,
    WorkerTrackerMixin as WorkerTrackerMixin,
    _ORPHANED_WORKERS as _ORPHANED_WORKERS,
    _accepts_is_cancelled as _accepts_is_cancelled,
    detach_running_worker as detach_running_worker,
)
from .presentation.app import (
    NAV_PAGES as NAV_PAGES,
    SINGLE_INSTANCE_SERVER_NAME as SINGLE_INSTANCE_SERVER_NAME,
    drain_pending_connections as drain_pending_connections,
    run_app as run_app,
)
from .presentation.lifecycle import FeatureMixin as FeatureMixin
from .presentation.shell import MainWindow as MainWindow, ShellMixin as ShellMixin
