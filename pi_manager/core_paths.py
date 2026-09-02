"""配置目录路径。从 ``core.py`` 抽出；对测试的 isolated_home 只在调用期读 HOME。"""
from __future__ import annotations

import os
from pathlib import Path

from . import secrets as secretstore



# ==== 基础工具：路径定位 / JSON 读写 / 敏感数据脱敏 ====


def user_home() -> Path:
    return Path(os.path.expanduser("~"))



def pi_agent_dir() -> Path:
    return secretstore.config_dir()



def models_path() -> Path:
    return pi_agent_dir() / "models.json"



def settings_path() -> Path:
    return pi_agent_dir() / "settings.json"



def auth_path() -> Path:
    return pi_agent_dir() / "auth.json"



def manager_config_path() -> Path:
    return pi_agent_dir() / "pi-manager.json"



def sessions_dir() -> Path:
    return pi_agent_dir() / "sessions"



def ensure_agent_dir() -> None:
    pi_agent_dir().mkdir(parents=True, exist_ok=True)
