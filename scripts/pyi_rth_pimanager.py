# Runtime hook for frozen PiManager builds.
# Ensures Qt can find bundled plugins next to the executable on all platforms.
from __future__ import annotations

import os
import sys
from pathlib import Path


def _set_qt_plugin_path() -> None:
    if not getattr(sys, "frozen", False):
        return
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        mp = Path(meipass)
        candidates.extend(
            [
                mp / "PySide6" / "plugins",
                mp / "PySide6" / "Qt" / "plugins",
                mp / "PySide6" / "Qt6" / "plugins",
            ]
        )
    exe_dir = Path(sys.executable).resolve().parent
    candidates.extend(
        [
            exe_dir / "_internal" / "PySide6" / "plugins",
            exe_dir / "_internal" / "PySide6" / "Qt" / "plugins",
            exe_dir / "_internal" / "PySide6" / "Qt6" / "plugins",
            exe_dir / "PySide6" / "plugins",
            # macOS app layout
            exe_dir / "PySide6" / "plugins",
        ]
    )
    if exe_dir.name == "MacOS":
        contents = exe_dir.parent
        candidates.extend(
            [
                contents / "Frameworks" / "PySide6" / "plugins",
                contents / "Resources" / "PySide6" / "plugins",
                contents / "MacOS" / "_internal" / "PySide6" / "plugins",
            ]
        )
    for path in candidates:
        if path.is_dir():
            os.environ.setdefault("QT_PLUGIN_PATH", str(path))
            # Prefer platform plugin directory existence as a soft signal.
            if (path / "platforms").is_dir():
                os.environ["QT_PLUGIN_PATH"] = str(path)
                break


_set_qt_plugin_path()
