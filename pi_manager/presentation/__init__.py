"""Modern presentation layer for Pi Manager."""

__all__ = ["ModernMainWindow"]


def __getattr__(name: str):
    if name == "ModernMainWindow":
        from .main_window import ModernMainWindow
        return ModernMainWindow
    raise AttributeError(name)
