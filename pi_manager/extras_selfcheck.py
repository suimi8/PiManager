# -*- coding: utf-8 -*-
"""桌面端自检（``--self-check`` / 诊断页）。

从 ``extras.py`` 下沉。``pi_manager.extras`` 继续 re-export，保持现有导入与
monkeypatch 点（``extras.xxx``）稳定。对会被测试 patch 的符号走 ``_extras().xxx``。
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from . import core
from . import secrets as secretstore


def _extras():
    from . import extras

    return extras


def run_self_check(
    is_cancelled: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """Return list of {name, ok, detail, level}."""
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, level: str = "info"):
        checks.append({"name": name, "ok": ok, "detail": detail, "level": level if ok else "warn"})

    def cancelled() -> bool:
        return bool(is_cancelled and is_cancelled())

    def stop_if_cancelled() -> bool:
        if not cancelled():
            return False
        add("自检进度", False, "已取消，仅显示已完成项", "warn")
        return True

    if stop_if_cancelled():
        return checks

    # Pi installed
    pi = core.find_pi_command()
    ver = core.get_installed_pi_version() or core.get_pi_version()
    add("Pi CLI", bool(pi), f"{pi or '未找到'} | 版本 {ver or '?'}", "error" if not pi else "info")

    # update available?
    try:
        info = core.needs_pi_install_or_update()
        needs_attention = any(
            info.get(key)
            for key in ("missing", "outdated", "repair_required", "blocked", "check_failed")
        )
        if needs_attention:
            add("Pi \u66f4\u65b0", False, info.get("message") or "\u9700\u8981\u5904\u7406", "warn")
        else:
            add("Pi \u66f4\u65b0", True, info.get("message") or "\u5df2\u662f\u517c\u5bb9\u901a\u9053\u6700\u65b0\u7248")
    except Exception as e:
        add("Pi 更新", True, f"跳过：{e}")

    # default model
    p, m, t = core.get_default_model()
    add("默认模型", bool(p and m), f"{p}/{m} thinking={t}" if p else "未设置", "warn" if not (p and m) else "info")

    # config dir
    d = core.pi_agent_dir()
    add("配置目录", d.exists(), str(d))

    # models.json
    models = core.load_models_config()
    provs = models.get("providers") or {}
    add("自定义 Provider", True, f"{len(provs)} 个")

    # proxy
    ps = _extras().get_proxy_settings()
    add(
        "代理",
        True,
        f"启用={ps['enabled']} url={ps['url'] or '—'} 环境={ps['env'] or '—'} 生效={ps['effective'] or '无'}",
    )

    # secrets
    names = secretstore.list_secret_names()
    backend = secretstore.backend_description()
    detail = f"{len(names)} 条（{backend}）"
    if secretstore.using_os_keyring():
        add("安全密钥库", True, detail)
    else:
        add(
            "安全密钥库",
            False,
            f"{detail}。机密性弱于系统钥匙串，见 SECURITY.md",
        )

    # orphaned provider key pools (provider config no longer in models.json)
    try:
        orphans = core.list_orphaned_provider_keys()
        if orphans:
            names_text = "、".join(o["provider"] for o in orphans[:5])
            more = f" 等 {len(orphans)} 个" if len(orphans) > 5 else ""
            add(
                "孤儿密钥",
                False,
                f"检测到 {len(orphans)} 个已无配置的 Provider 密钥池：{names_text}{more}（可在 Provider 页一键清理）",
                "warn",
            )
        else:
            add("孤儿密钥", True, "无")
    except Exception as e:
        add("孤儿密钥", True, f"跳过：{e}")

    # stale settings.enabledModels references (removed providers)
    if stop_if_cancelled():
        return checks
    try:
        builtin: set[str] = set()
        if core.find_pi_command():
            for m in core.list_models():
                builtin.add(m.provider)
        stale = core.list_stale_enabled_models(builtin_providers=builtin)
        if stale:
            stale_text = "、".join(stale[:5])
            more = f" 等 {len(stale)} 条" if len(stale) > 5 else ""
            add(
                "启用模型残留",
                False,
                f"settings.enabledModels 引用了 {len(stale)} 个已不存在的模式：{stale_text}{more}；"
                "Pi 每次启动都会输出 No models match pattern 警告，建议清理",
                "warn",
            )
        else:
            add("启用模型残留", True, "无残留模式")
    except Exception as e:
        add("启用模型残留", True, f"跳过：{e}")

    # third-party config sources (e.g. pi-ui writes models-store.json)
    try:
        store_path = core.pi_agent_dir() / "models-store.json"
        if store_path.exists():
            count = ""
            try:
                store_data = json.loads(
                    store_path.read_text(encoding="utf-8-sig")
                )
                if isinstance(store_data, dict):
                    count = f"（{len(store_data.get('providers') or {})} 个 provider）"
            except Exception:
                pass
            add(
                "第三方配置源",
                True,
                f"检测到 models-store.json{count}——由其他工具（如 pi-ui）维护，"
                "PiManager 不读写该文件；如两处配置不同步，请以 models.json 为准",
                "info",
            )
        else:
            add("第三方配置源", True, "无")
    except Exception as e:
        add("第三方配置源", True, f"跳过：{e}")

    # Pi project trust file (managed by the pi CLI itself)
    try:
        trust_path = core.pi_agent_dir() / "trust.json"
        if trust_path.exists():
            trust_data = json.loads(trust_path.read_text(encoding="utf-8-sig"))
            entries = "；".join(f"{k} → {v}" for k, v in trust_data.items()) or "空"
            add("项目信任", True, f"Pi 信任列表：{entries[:120]}")
        else:
            add("项目信任", True, "未配置")
    except Exception:
        add("项目信任", True, "存在但无法解析")

    # language
    add("语言偏好", True, core.get_language())

    # workdir last
    mgr = core.load_manager_config()
    wd = mgr.get("last_workdir") or ""
    add("最近工作目录", bool(wd), str(wd) or "—")

    # network quick (optional lightweight); several endpoints so users outside
    # mainland China are not misreported as offline. Consistent HTTP policy:
    # no redirects, status only, body never read.
    if stop_if_cancelled():
        return checks
    import urllib.parse
    import urllib.request

    from . import http_client

    probe_urls = (
        "https://www.baidu.com",
        "https://www.gstatic.com/generate_204",
        "https://api.github.com",
    )
    probe_error = ""
    for probe_url in probe_urls:
        if cancelled():
            add("基础网络", False, "已取消，未完成连通探测", "warn")
            add("Pi Manager 版本", True, _extras().APP_VERSION)
            return checks
        try:
            t0 = time.perf_counter()
            req = urllib.request.Request(
                probe_url, method="GET", headers={"User-Agent": "PiManager"}
            )
            opener = urllib.request.build_opener(http_client.DenyRedirectHandler())
            with opener.open(req, timeout=5) as resp:
                _ = resp.status
            ms = round((time.perf_counter() - t0) * 1000)
            host = urllib.parse.urlsplit(probe_url).netloc
            add("基础网络", True, f"连通（{host}），延迟约 {ms} ms")
            break
        except Exception as e:
            probe_error = str(e)
    else:
        add("基础网络", False, f"异常：{probe_error}", "warn")

    add("Pi Manager 版本", True, _extras().APP_VERSION)
    return checks
