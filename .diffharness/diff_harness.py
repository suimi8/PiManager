# -*- coding: utf-8 -*-
"""core_remote 拆分等价性差分验证（老实现 vs 新实现，端到端）。

把拆分前的 core_remote.py 原样放进包内当参考模块（_ref_core_remote），用同一批
输入分别驱动两个模块，逐字段比对返回的 dict 与实际发出的请求。不进网络：伪造
opener / _http_json_request。仅用于本地验证，不进仓库（.diffharness 用后删除）。
"""
from __future__ import annotations

import io
import itertools
import json
import sys
import urllib.error
import urllib.request

REPO = sys.argv[1]
sys.path.insert(0, REPO)

from pi_manager import core                          # noqa: E402
from pi_manager import core_remote as new_mod        # noqa: E402


def _load_ref_module():
    """加载拆分前的 core_remote 参考基线（_ref_core_remote）。

    参考文件已从 pi_manager/ 包内移除（避免死代码留在生产包中）；这里优先
    尝试直接导入，失败时用 git show 从历史恢复临时副本，并以 pi_manager 包内
    子模块身份加载（该文件使用相对导入，必须挂在包名下）。
    """
    try:
        from pi_manager import _ref_core_remote as ref_mod  # noqa: F401

        return ref_mod
    except ImportError:
        pass
    import importlib.util
    import os
    import subprocess
    import tempfile

    tmp = tempfile.mkdtemp(prefix="pi-ref-")
    path = os.path.join(tmp, "_ref_core_remote.py")
    with open(path, "w", encoding="utf-8") as fh:
        subprocess.run(
            ["git", "show", "HEAD:pi_manager/_ref_core_remote.py"],
            cwd=REPO,
            check=True,
            stdout=fh,
        )
    spec = importlib.util.spec_from_file_location(
        "pi_manager._ref_core_remote",
        path,
        submodule_search_locations=[],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["pi_manager._ref_core_remote"] = module
    spec.loader.exec_module(module)
    return module


ref_mod = _load_ref_module()

core._effective_proxy_url = lambda p: ""


PAYLOADS = [
    '{"data":[{"id":"gpt-4o"},{"id":"gpt-4o-mini","context_window":128000}]}',
    '{"object":"list","data":[{"id":"gpt-4","name":"GPT-4","max_tokens":"8K"}]}',
    '{"data":["str-model-a","str-model-b"]}',
    '{"data":[{"name":"only-name"},{"id":""},{},null,123]}',
    '{"data":[]}',
    '{"data":[{"id":"claude-3-5-sonnet","display_name":"Claude","type":"model"}],"has_more":false}',
    '{"models":[{"name":"models/gemini-2.0-flash","displayName":"Gemini","inputTokenLimit":1048576}]}',
    '{"models":[{"displayName":"no-name"},{"name":""},{"name":"models/x","contextWindow":"1M"},"str"]}',
    '{"models":[]}',
    '{"data":[{"id":"deepseek/deepseek-chat","name":"DS","context_length":64000,"context_window":"64K"}]}',
    '[{"id":"glm-4-plus","maxTokens":"128K"},{"id":"glm-4-flash"}]',
    '["glm-4","glm-3-turbo"]',
    '[1,2,null,{"nope":1}]',
    '[]',
    '{}',
    '{"data":"not-a-list"}',
    '{"models":{"a":1}}',
    '{"data":[{"id":"x","context_window":"unknown","max_tokens":"unlimited"}]}',
    '{"data":[{"id":"y","contextWindow":true,"maxTokens":3.9}]}',
    '{"data":[{"id":"dup"},{"id":"dup"},{"id":"dup2"}]}',
    '{"data":[{"id":"g","context_window":"12G"},{"id":"m","max_tokens":"7M"}]}',
    'not json at all',
    '',
    'null',
    '"a string"',
    '123',
]

APIS = [
    "openai-completions", "openai", "openai-responses",
    "anthropic-messages", "anthropic",
    "google-generative-ai", "google",
    "ollama", "", "OpenAI-Completions",
]

BASES = [
    "https://api.openai.com/v1",
    "https://api.openai.com/v1/",
    "https://api.openai.com",
    "https://api.openai.com/v1/chat/completions",
    "https://x.example/v1beta",
    "https://x.example/models",
    "https://generativelanguage.googleapis.com/v1beta",
    "https://g.example/v1beta/models?key=INLINE",
    "https://api.anthropic.com/v1",
    "https://api.anthropic.com",
    "https://api.anthropic.com/v1/messages",
    "http://127.0.0.1:11434/v1",
    "https://proxy.example/api/v1/openai",
    "",
    "ftp://bad.example/v1",
    "file:///etc/passwd",
]

STATUSES = [200, 401, 429, 500]


def _make_opener(payload, status, recorder):
    class Response:
        headers = {"Content-Length": str(len(payload.encode()))}
        status = 200

        def read(self, n=-1):
            return payload.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Opener:
        def open(self, request, timeout=None):
            recorder.append(
                (request.full_url, dict(request.header_items()), request.get_method())
            )
            if status == 200:
                return Response()
            raise urllib.error.HTTPError(
                request.full_url, status, "Err", {}, io.BytesIO(payload.encode("utf-8"))
            )

    return Opener()


def run_fetch(mod, base, key, api, payload, status):
    rec = []
    orig = urllib.request.build_opener
    urllib.request.build_opener = lambda *h: _make_opener(payload, status, rec)
    try:
        try:
            out = mod.fetch_remote_models(base, key, api=api)
        except Exception as exc:
            out = {"__exc__": type(exc).__name__ + ": " + str(exc)}
    finally:
        urllib.request.build_opener = orig
    return out, rec


def diff_fetch():
    bad = 0
    total = 0
    for base, api in itertools.product(BASES, APIS):
        for payload, status in itertools.product(PAYLOADS, STATUSES):
            total += 1
            a, ra = run_fetch(ref_mod, base, "sk-diff-secret", api, payload, status)
            b, rb = run_fetch(new_mod, base, "sk-diff-secret", api, payload, status)
            if a != b or ra != rb:
                bad += 1
                if bad <= 6:
                    print("FETCH MISMATCH", repr(base), api, repr(payload[:60]), status)
                    print("  ref:", json.dumps(a, ensure_ascii=False, default=str)[:600])
                    print("  new:", json.dumps(b, ensure_ascii=False, default=str)[:600])
                    if ra != rb:
                        print("  req ref:", ra)
                        print("  req new:", rb)
    for base, api in itertools.product(BASES, APIS):
        for key in ("", "OPENAI_API_KEY", "  ", "sk-x"):
            total += 1
            a, ra = run_fetch(ref_mod, base, key, api, '{"data":[]}', 200)
            b, rb = run_fetch(new_mod, base, key, api, '{"data":[]}', 200)
            if a != b or ra != rb:
                bad += 1
                if bad <= 12:
                    print("FETCH-KEY MISMATCH", repr(base), api, repr(key))
                    print("  ref:", a)
                    print("  new:", b)
    print("fetch_remote_models: " + str(total) + " 组，" + str(bad) + " 处不一致")
    return bad


PREVIEW_BODIES = [
    '{"choices":[{"message":{"content":"OK"}}]}',
    '{"choices":[{"message":{"content":[{"type":"text","text":"A"},{"type":"text","text":"B"}]}}]}',
    '{"choices":[{"message":{"content":["x","y",{"text":"z"},{"nope":1},7]}}]}',
    '{"choices":[{"message":{"content":""}},{"message":{"content":"second"}}]}',
    '{"choices":[{"message":{}}]}',
    '{"choices":[{"message":{"content":null},"text":"legacy-completion"}]}',
    '{"choices":[]}',
    '{"choices":[{"text":"only-text"}]}',
    '{"output_text":"responses-flat"}',
    '{"output":[{"content":[{"text":"resp-nested"}]}]}',
    '{"output":[{"content":[]},{"content":[{"text":"second-item"}]}]}',
    '{"output":["str",{"content":"notalist"},{"content":[{"nope":1}]}]}',
    '{"output_text":""}',
    '{"content":[{"type":"text","text":"anthropic-1"}]}',
    '{"content":[{"type":"thinking","thinking":"..."},{"type":"text","text":"anthropic-2"}]}',
    '{"content":[{"text":"no-type-field"}]}',
    '{"content":[]}',
    '{"content":"a string"}',
    '{"candidates":[{"content":{"parts":[{"text":"g1"},{"text":"g2"}]}}]}',
    '{"candidates":[{"content":{"parts":[]}}]}',
    '{"candidates":[{"content":{}}]}',
    '{"candidates":[]}',
    '{"candidates":[{"content":{"parts":[{"nope":1},"str"]}}]}',
    '{"candidates":[{"content":{"parts":"notalist"}}]}',
    '{"error":{"message":"boom"}}',
    '{"choices":[{"message":{"content":"c"}}],"content":[{"type":"text","text":"both"}]}',
    '{"content":[{"type":"text","text":"anth"}],"candidates":[{"content":{"parts":[{"text":"goog"}]}}]}',
    '{}', '[]', 'null', '"str"', '123', 'not json', '',
    '{"choices":[{"message":{"content":"' + "x" * 400 + '"}}]}',
    '{"content":[{"type":"text","text":"  padded  "}]}',
    '{"choices":"not-a-list"}',
    '{"choices":[null]}',
    '{"choices":[{"message":"not-a-dict"}]}',
    '{"output":"not-a-list"}',
]


def diff_preview():
    bad = 0
    total = 0
    for api in APIS + ["anything-else", None]:
        for body in PREVIEW_BODIES:
            for limit in (120, 5, 0, 1000):
                total += 1
                try:
                    a = ref_mod._extract_reply_preview(api, body, limit)
                except Exception as exc:
                    a = "__exc__" + type(exc).__name__ + ":" + str(exc)
                try:
                    b = new_mod._extract_reply_preview(api, body, limit)
                except Exception as exc:
                    b = "__exc__" + type(exc).__name__ + ":" + str(exc)
                if a != b:
                    bad += 1
                    if bad <= 8:
                        print("PREVIEW MISMATCH", api, repr(body[:70]), limit)
                        print("  ref:", repr(a))
                        print("  new:", repr(b))
    print("_extract_reply_preview: " + str(total) + " 组，" + str(bad) + " 处不一致")
    return bad


RESULTS = [
    {"ok": True, "status": 200, "body": '{"choices":[{"message":{"content":"OK"}}]}',
     "latency_ms": 1.0, "proxy": "", "error": ""},
    {"ok": False, "status": 401, "body": '{"error":{"message":"invalid api key"}}',
     "latency_ms": 2.0, "proxy": "", "error": "HTTP 401: Unauthorized"},
    {"ok": False, "status": 415,
     "body": 'Unsupported content type, expected application/json',
     "latency_ms": 3.0, "proxy": "", "error": "HTTP 415: Unsupported Media Type"},
    {"ok": False, "status": 0, "body": "", "latency_ms": 4.0, "proxy": "", "error": "timed out"},
    {"ok": True, "status": 200, "body": '{"candidates":[{"content":{"parts":[{"text":"G"}]}}]}',
     "latency_ms": 5.0, "proxy": "", "error": ""},
    {"ok": True, "status": 200, "body": '{"content":[{"type":"text","text":"A"}]}',
     "latency_ms": 6.0, "proxy": "", "error": ""},
]

HEADER_SETS = [
    {},
    {"Authorization": "Bearer literal-header"},
    {"authorization": "Bearer lowercase-header"},
    {"User-Agent": "Custom/1.0"},
    {"anthropic-version": "2024-01-01"},
    {"X-Custom": "plain-value"},
]

_STATE = {"result": RESULTS[0]}


def diff_test_model_http():
    bad = 0
    total = 0
    captured = []

    def fake_request(url, *, method="GET", headers=None, body=None, **kw):
        captured.append((url, dict(headers or {}), method, body))
        return dict(_STATE["result"])

    core._http_json_request = fake_request

    for base, api in itertools.product(BASES, APIS):
        for key in ("sk-http-secret", ""):
            for extra in HEADER_SETS:
                for res in RESULTS:
                    total += 1
                    _STATE["result"] = res
                    entry = {"baseUrl": base, "api": api, "apiKey": key,
                             "headers": dict(extra)}
                    core.get_provider_config = lambda p, _e=entry: dict(_e)
                    captured.clear()
                    try:
                        a = ref_mod.test_model_http("Diff", "model-x")
                    except Exception as exc:
                        a = {"__exc__": type(exc).__name__ + ": " + str(exc)}
                    ca = list(captured)
                    captured.clear()
                    try:
                        b = new_mod.test_model_http("Diff", "model-x")
                    except Exception as exc:
                        b = {"__exc__": type(exc).__name__ + ": " + str(exc)}
                    cb = list(captured)
                    if a != b or ca != cb:
                        bad += 1
                        if bad <= 8:
                            print("HTTP MISMATCH", repr(base), api, repr(key), extra,
                                  res["status"])
                            print("  ref:", json.dumps(a, ensure_ascii=False, default=str)[:500])
                            print("  new:", json.dumps(b, ensure_ascii=False, default=str)[:500])
                            if ca != cb:
                                print("  req ref:", ca)
                                print("  req new:", cb)
    print("test_model_http: " + str(total) + " 组，" + str(bad) + " 处不一致")
    return bad


if __name__ == "__main__":
    bad = diff_preview() + diff_fetch() + diff_test_model_http()
    print("=" * 60)
    print("TOTAL MISMATCHES:", bad)
    sys.exit(1 if bad else 0)
