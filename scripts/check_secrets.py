#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仓库密钥/敏感文件扫描（零第三方依赖）。

检测两类泄漏：

1. 文件名黑名单：``secrets.vault``、``auth.json``、``*.pem``、``*.key``、
   ``id_rsa*``、``*.pfx``、``*.p12``、``.env``、``*.keystore`` 等。
2. 文件内容模式：真实 API Key / 私钥 / Bearer 形态（``sk-``、``ghp_``、
   ``AKIA``、``xoxb-``、``AIza``、PEM 私钥块、``bearer <token>`` 等）。

用法::

    python scripts/check_secrets.py                 # 扫描仓库（跳过 tests/）
    python scripts/check_secrets.py --scan-tests    # 包含 tests/（用于 PR 双确认）
    python scripts/check_secrets.py --path <dir>    # 指定目录

规则：

- 默认只扫描 ``git ls-files`` 跟踪的文件（避免 dist/build/node_modules 噪声）。
- 默认跳过 ``tests/``：测试文件允许模拟密钥（``sk-first-secret`` 等），
  但跳过不代表豁免——CI 的 secret-scan job 会加 ``--scan-tests`` 双确认，
  且测试中的密钥均为明显假值（带连字符语义名或 sk-test 前缀）。
- 退出码 0 = 无泄漏；1 = 发现泄漏；2 = 用法/环境错误。

与 ``docs/DEVELOPMENT_STANDARDS.md``「密钥红线」配套：CI 上由
``secret-scan`` job 强制执行本脚本。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# CI Windows runner 的 stdout 默认 cp1252，中文输出会触发 UnicodeEncodeError；
# 统一强制 UTF-8（errors=replace 兜底），保证跨平台一致。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]

# 文件名黑名单（大小写不敏感；匹配 basename）
_BAD_FILE_NAMES = {
    "secrets.vault",
    "secrets.dpapi",
    "auth.json",
    ".env",
    ".npmrc",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "credentials.json",
    ".netrc",
    ".pypirc",
    ".htpasswd",
    ".pgpass",
}
_BAD_FILE_SUFFIXES = {".pem", ".key", ".pfx", ".p12", ".keystore", ".jks", ".ppk"}

# 内容模式：只匹配"真实密钥"形态，避免误报测试模拟值。
# 注意顺序：先匹配更具体的模式。``sk-`` 要求后接 16+ 位字母数字（真实 key 无连字符）。
_SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{12,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    re.compile(r"\bbearer\s+[A-Za-z0-9._-]{16,}\b", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),  # JWT
]

# 明显测试/示例假值子串：命中即视为模拟密钥，不报告。
_INNOCENT_TOKEN_PARTS = (
    "sk-test", "sk-first", "sk-second", "sk-bad", "sk-demo", "sk-sanitize",
    "sk-process", "sk-zhipu", "dummy", "example", "placeholder", "redacted",
    "custom-secret", "custom-header-secret", "test-secret", "fake", "sample",
    "secret-value", "sk-1234567890",
)

# 跳过内容检查的文件（构建产物、文档截图占位等）
_SKIP_CONTENT_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".woff", ".woff2"}
_SKIP_CONTENT_NAMES = {"package-lock.json"}

# 已知合法用途豁免：文件路径 -> 允许出现的模式（正则字符串）。
# pi-sensitive-guard 是密钥检测守卫扩展，源码内必须包含 PEM 私钥正则。
_ALLOWED_SECRETS: dict[str, set[str]] = {
    "assets/builtin/extensions/pi-sensitive-guard/index.ts": {
        r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----",
    },
}


def _tracked_files(root: Path, scan_tests: bool) -> list[Path]:
    """返回 git 跟踪的文件列表；非 git 仓库时回退目录遍历。"""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(root),
            capture_output=True,
            check=True,
        )
        names = out.stdout.decode("utf-8", errors="replace").split("\0")
        files = [root / name for name in names if name]
    except (OSError, subprocess.CalledProcessError):
        files = [p for p in root.rglob("*") if p.is_file()]
    if not scan_tests:
        files = [p for p in files if "tests" not in p.parts]
    return files


def _check_file(path: Path) -> list[tuple[str, str]]:
    """返回 [(匹配的 pattern 字符串, 展示片段), ...]。"""
    findings: list[tuple[str, str]] = []
    name = path.name
    lowered = name.lower()
    if lowered in _BAD_FILE_NAMES:
        findings.append(("<filename>", f"文件名黑名单: {name}"))
    for suffix in _BAD_FILE_SUFFIXES:
        if lowered.endswith(suffix):
            findings.append(("<filename>", f"文件名黑名单（后缀）: {name}"))
            break
    if (
        lowered in _SKIP_CONTENT_NAMES
        or lowered.endswith(tuple(_SKIP_CONTENT_SUFFIXES))
    ):
        return findings
    try:
        data = path.read_bytes()
    except OSError:
        return findings
    if b"\x00" in data[:8192]:
        return findings  # 二进制文件跳过内容检查
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return findings
    for pattern in _SECRET_PATTERNS:
        match = pattern.search(text)
        if match:
            snippet = match.group(0)
            lowered_snippet = snippet.lower()
            if any(part in lowered_snippet for part in _INNOCENT_TOKEN_PARTS):
                continue  # 测试/示例模拟密钥
            if len(snippet) > 24:
                snippet = snippet[:12] + "…" + snippet[-8:]
            findings.append((pattern.pattern, f"疑似密钥内容: {pattern.pattern}（{snippet}）"))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=str(REPO_ROOT), help="扫描根目录（默认仓库根）")
    parser.add_argument("--scan-tests", action="store_true", help="包含 tests/ 目录")
    args = parser.parse_args()

    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"目录不存在: {root}", file=sys.stderr)
        return 2

    files = _tracked_files(root, args.scan_tests)
    violations: list[tuple[str, str]] = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        allowed = _ALLOWED_SECRETS.get(rel, set())
        for pattern_str, finding in _check_file(path):
            if pattern_str in allowed:
                continue  # 已知合法用途（如密钥守卫的正则定义）
            violations.append((rel, finding))

    if violations:
        print(f"发现 {len(violations)} 处密钥/敏感文件泄漏：")
        for rel, finding in sorted(violations):
            print(f"  - {rel}: {finding}")
        print(
            "\n请立即从仓库移除（含 git 历史中的版本），并轮换已泄漏的密钥。"
        )
        return 1
    print(f"OK：扫描 {len(files)} 个跟踪文件，未发现密钥/敏感文件泄漏。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
