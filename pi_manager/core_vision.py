# -*- coding: utf-8 -*-
"""视觉识图管道：智谱 GLM 免费视觉模型把图片转文字，供 Pi vision skill 使用。

从 ``core.py`` 抽出的 vision 子系统。对 core 配置函数（load/save_manager_config、
validate_proxy_url、_extract_reply_preview、DEFAULT_OPENAI_COMPAT_USER_AGENT、
_effective_proxy_url）通过函数内延迟 import 引用，避免循环依赖。
``core.py`` 在顶部重新导出这些符号以保持 ``core.xxx`` 调用兼容。
"""
from __future__ import annotations

import base64
import json
import logging
import os

from .core_http import _ssl_context

logger = logging.getLogger(__name__)


# ---- vision / image understanding (built-in Zhipu GLM-4.6V-Flash) -------


ZHIPU_VISION_MODELS = ("glm-4.6v-flash", "glm-4.1v-thinking-flash")
ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
ZHIPU_API_KEY_SECRET = "zhipu:apiKey"


def zhipu_api_key() -> str:
    """Return the configured Zhipu API key (secret store first, then env)."""
    try:
        from . import secrets as secretstore

        value = secretstore.get_secret(ZHIPU_API_KEY_SECRET)
        if value:
            return value
    except Exception:
        pass
    return os.environ.get("ZHIPU_API_KEY", "").strip()


def set_zhipu_api_key(value: str) -> None:
    """Persist the Zhipu API key into the secure secret store."""
    from . import secrets as secretstore

    value = (value or "").strip()
    if value:
        secretstore.set_secret(ZHIPU_API_KEY_SECRET, value)
    else:
        secretstore.delete_secret(ZHIPU_API_KEY_SECRET)
    try:
        from . import builtin_plugins
        builtin_plugins.install_all_builtins()
    except Exception:
        pass


def vision_model_choice() -> str:
    """Return the user-configured vision model name ('' = auto)."""
    try:
        from . import core

        cfg = core.load_manager_config()
        return str(cfg.get("vision_model") or "").strip()
    except Exception:
        return ""


def set_vision_model_choice(value: str) -> None:
    """Persist the vision model choice ('' = auto) into pi-manager.json."""
    try:
        from . import core

        cfg = core.load_manager_config()
        cfg["vision_model"] = (value or "").strip()
        core.save_manager_config(cfg)
    except Exception:
        pass


def _call_zhipu_vision(
    model: str,
    api_key: str,
    body_obj: dict,
    timeout: float,
) -> dict:
    """Call the Zhipu vision endpoint.

    Zhipu is a mainland-China API: it is tried with an explicit NO-PROXY
    opener first (``urllib.urlopen`` alone would silently honor HTTP_PROXY /
    HTTPS_PROXY env vars and fail with connection-refused when the proxy is
    down). The proxy is only used as a fallback when the direct attempt fails
    at the network layer (no server response); service responses such as 429
    mean the network is fine and must be returned as-is so the caller can
    switch to the backup model.
    """
    result = _zhipu_vision_request(model, api_key, body_obj, timeout, proxy="")
    if result.get("ok") or result.get("http_status"):
        # A server answered (even with an error): the network is fine.
        return result
    proxy = _configured_proxy_url()
    if not proxy:
        return result
    return _zhipu_vision_request(model, api_key, body_obj, timeout, proxy=proxy)


def _configured_proxy_url() -> str:
    try:
        from . import core

        cfg = core.load_manager_config()
        if cfg.get("proxy_enabled") and cfg.get("proxy_url"):
            url = str(cfg.get("proxy_url") or "").strip()
            error = core.validate_proxy_url(url)
            if error:
                logger.warning("已配置的代理地址无效，已忽略: %s", error)
                return ""
            return url
    except Exception as exc:
        logger.warning("读取代理配置失败: %s", exc)
    return ""


def _zhipu_vision_request(
    model: str,
    api_key: str,
    body_obj: dict,
    timeout: float,
    proxy: str,
) -> dict:
    import time as _time
    import urllib.error
    import urllib.request

    from . import core
    from . import http_client

    req = urllib.request.Request(
        ZHIPU_BASE_URL + "/chat/completions",
        data=json.dumps(body_obj, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": core.DEFAULT_OPENAI_COMPAT_USER_AGENT,
        },
        method="POST",
    )
    handlers: list = []
    if proxy:
        handlers.append(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    else:
        # Explicitly disable proxies (including env vars) for the direct path.
        handlers.append(urllib.request.ProxyHandler({}))
    handlers.append(urllib.request.HTTPSHandler(context=_ssl_context(False)))
    opener = urllib.request.build_opener(*handlers)
    t0 = _time.perf_counter()
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = http_client.read_limited(resp, http_client.MODEL_TEST_MAX_BYTES)
            status = int(getattr(resp, "status", 200))
            body_text = raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            # 读取错误响应体仅用于触发 read_limited 的大小/编码校验，内容不回传。
            http_client.read_limited(e, http_client.ERROR_MAX_BYTES)
        except Exception:
            pass
        return {
            "ok": False,
            "description": "",
            "error": f"HTTP {e.code}: {e.reason}",
            "http_status": int(getattr(e, "code", 0) or 0),
            "model": model,
            "latency_ms": round((_time.perf_counter() - t0) * 1000, 1),
        }
    except Exception as e:
        return {
            "ok": False,
            "description": "",
            "error": str(e),
            "http_status": 0,
            "model": model,
            "latency_ms": round((_time.perf_counter() - t0) * 1000, 1),
        }
    if not (200 <= status < 300):
        return {
            "ok": False,
            "description": "",
            "error": f"HTTP {status}",
            "http_status": status,
            "model": model,
            "latency_ms": round((_time.perf_counter() - t0) * 1000, 1),
        }
    description = core._extract_reply_preview("openai-completions", body_text or "", limit=12000)
    if not description:
        return {"ok": False, "description": "", "error": "识图模型没有返回文本", "model": model}
    return {
        "ok": True,
        "description": description,
        "latency_ms": round((_time.perf_counter() - t0) * 1000, 1),
        "model": model,
    }


def build_vision_prompt(user_prompt: str = "") -> str:
    """Build a targeted vision instruction from the user's question.

    Screenshots almost always carry text (code, errors, UI labels); the vision
    model must transcribe it verbatim instead of giving a generic description,
    and it should focus on details relevant to the user's question.
    """
    user_prompt = (user_prompt or "").strip()
    if user_prompt:
        return (
            "请仔细阅读这张图片，完成以下任务：\n"
            "1. 原样转录图片中的全部文字内容（代码、报错信息、界面文本、按钮文案等），"
            "不要遗漏、不要改写、不要概括；\n"
            "2. 简要说明图片类型与整体布局（如：终端报错截图 / 代码编辑器 / 对话框）；\n"
            "3. 针对用户的问题，提取图片中与之相关的关键信息。\n"
            f"用户的问题：{user_prompt}"
        )
    return (
        "请仔细阅读这张图片：原样转录其中的全部文字（代码、报错、界面文本），"
        "并简要描述图片类型与整体布局。"
    )


# ==== 视觉：智谱识图 / 图片校验 / 技能安装 ====


_ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff"}
_MAX_IMAGE_BYTES = 20 * 1024 * 1024


def load_image_for_describe(path: str) -> dict:
    """Validate + read an image file for ``--vision-describe`` (no GUI deps).

    Returns ``{"ok": True, "data": bytes}`` on success, or
    ``{"ok": False, "error": <中文错误>}`` otherwise. Error strings and
    acceptance rules (extension whitelist, 20 MB cap) are the single source
    of truth for the CLI hot path so behavior cannot drift.
    """
    p = os.path.normpath(os.path.abspath(os.path.expanduser(path)))
    if os.path.splitext(p)[1].lower() not in _ALLOWED_IMAGE_EXTS:
        return {"ok": False, "error": "仅支持图片文件（png/jpg/jpeg/gif/bmp/webp/tiff）"}
    try:
        if os.path.getsize(p) > _MAX_IMAGE_BYTES:
            return {"ok": False, "error": "图片文件过大（上限 20MB）"}
    except OSError as exc:
        return {"ok": False, "error": f"无法读取图片：{exc}"}
    try:
        with open(p, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        return {"ok": False, "error": f"无法读取图片：{exc}"}
    return {"ok": True, "data": data}


def describe_image(
    image_bytes: bytes,
    mime: str = "image/png",
    prompt: str = "请详细描述这张图片的内容，包括界面元素、文字与布局。",
    timeout: float = 90,
    model: str | None = None,
) -> dict:
    """Describe an image with the built-in free Zhipu vision models.

    Model selection:
    - explicit ``model`` argument wins;
    - otherwise the user-configured choice (settings page);
    - otherwise the automatic chain: ``glm-4.6v-flash`` first, falling back to
      ``glm-4.1v-thinking-flash`` when the free tier is rate-limited (429).

    Requires a Zhipu API key (settings page or ``ZHIPU_API_KEY`` env var).
    Returns ``{ok, description, error, ...}``.
    """
    api_key = zhipu_api_key()
    if not api_key:
        return {
            "ok": False,
            "description": "",
            "error": (
                "未配置智谱 API Key。请在「设置 → 识图模型」填入，免费申请："
                "https://bigmodel.cn （GLM-4.6V-Flash / GLM-4.1V-Thinking-Flash 免费额度）"
            ),
        }
    configured = model or vision_model_choice()
    candidates = [configured] if configured else list(ZHIPU_VISION_MODELS)
    data_uri = (
        f"data:{mime or 'image/png'};base64,"
        f"{base64.b64encode(image_bytes or b'').decode('ascii')}"
    )
    body_obj: dict = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            }
        ],
        # GLM-4.1V-Thinking spends part of the budget on its chain-of-thought;
        # a small cap truncates the transcription mid-sentence.
        "max_tokens": 8192,
    }
    last: dict | None = None
    for candidate in candidates:
        result = _call_zhipu_vision(
            candidate,
            api_key,
            {**body_obj, "model": candidate},
            timeout,
        )
        if result.get("ok"):
            return result
        last = result
        # Rate-limited on the free tier: try the next free model.
        if result.get("http_status") not in (429, 500, 502, 503):
            return result
    return last or {"ok": False, "description": "", "error": "识图失败"}


def _make_test_image_png(size: int = 64) -> bytes:
    """Generate a solid red PNG without any external image dependency."""
    import struct
    import zlib

    width = height = size
    row = b"\x00" + b"\xff\x00\x00" * width  # filter 0, RGB red

    def chunk(tag: bytes, data: bytes) -> bytes:
        payload = tag + data
        return (
            struct.pack(">I", len(data))
            + payload
            + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


def test_vision(timeout: float = 90) -> dict:
    """Verify the built-in vision pipeline with a generated solid red image.

    Returns the describe_image result; a healthy model should answer with a
    red-ish color name.
    """
    png = _make_test_image_png(64)
    result = describe_image(
        png,
        "image/png",
        prompt="这张图片是什么颜色？请直接回答颜色名。",
        timeout=timeout,
    )
    result["test_image"] = "红色测试图（程序生成）"
    return result


def ensure_zhipu_provider() -> dict:
    """校验智谱识图配置是否就绪（仅用于识图管道，不写 models.json）。

    设置页配置的智谱 API Key 与识图模型选择（GLM-4.6V-Flash /
    GLM-4.1V-Thinking-Flash）只服务于识图管道：describe_image 与 Pi vision
    skill（--vision-describe）默认使用它们把图片转为文字。这些模型**不会**
    自动出现在 provider 模型列表中；用户如需在列表中使用智谱模型，请在
    Provider 管理中手动添加。
    """
    key = zhipu_api_key()
    if not key:
        raise ValueError(
            "未配置智谱 API Key。请在「设置 → 识图模型」填入（免费申请：https://bigmodel.cn）"
        )
    return {
        "ok": True,
        "api_key_configured": True,
        "base_url": ZHIPU_BASE_URL,
        "models": list(ZHIPU_VISION_MODELS),
    }


def install_vision_skill() -> dict:
    """安装 / 刷新 Pi vision skill 到 ``~/.pi/agent/skills/pi-manager-vision/``。

    现已委托给 ``builtin_plugins`` 统一机制：资源源文件位于
    ``assets/builtin/skills/pi-manager-vision/SKILL.md.tmpl``，落盘时渲染
    ``{{vision_command}}`` 占位。保留本函数仅为向后兼容（旧调用点与测试）。
    """
    try:
        from . import builtin_plugins
        return builtin_plugins.install_builtin("pi-manager-vision")
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
