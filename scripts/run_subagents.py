#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""并行运行多个 pi 子代理的调度脚本（多子代理编排）。

原理与 pi 官方 subagent 扩展一致：每个子代理是一个独立的 ``pi -p``
非交互进程，拥有隔离的上下文窗口。本脚本只负责并发启动、日志采集、
超时控制和状态汇总，不干预子代理内部行为。

用法::

    python scripts/run_subagents.py --tasks tasks.json [--concurrency 3] [--timeout-min 30]

tasks.json 结构::

    [
      {
        "name": "s1-security",                     # 子代理名（用于日志/报告命名）
        "model": "sub2api-cpolar/glm-5-2",         # 模型（provider/id）
        "task_file": "work/.subagents/tasks/s1.md", # 任务文本 md 文件（避免 JSON 转义问题）
        "out_file": "docs/review/s1-security.md",  # 子代理负责写入的报告
        "timeout_min": 30                           # 单个子代理超时（分钟）
      }
    ]

    task 与 task_file 二选一：task 直接给文本，task_file 从文件读取。

日志输出到 ``work/.subagents/<name>.log``（work/ 已被 .gitignore 忽略）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = REPO_ROOT / "work" / ".subagents"
PI_MODEL_DEFAULT = "opencode go/deepseek-v4-flash"
THINKING = "max"


def _find_pi_launcher() -> tuple[str, str]:
    """定位 node 可执行文件与 pi 的 cli.js，避免经过 bash/shell 解析。

    Windows Python 的 subprocess 通过 CreateProcess 查找 bash 时可能命中
    WSL 的 bash（其 node 版本过老），因此这里直接定位 node 与 cli.js。
    """
    import shutil
    import sys

    node = shutil.which("node") or "node"
    candidates: list[Path] = []
    # 1) 从 npm 全局 bin 目录推导
    npm_bin = shutil.which("pi") or shutil.which("pi.cmd")
    if npm_bin:
        candidates.append(Path(npm_bin).resolve().parent / "node_modules" / "@earendil-works" / "pi-coding-agent" / "dist" / "cli.js")
    # 2) 常见全局安装位置
    candidates.extend([
        Path(sys.prefix) / "node_modules" / "@earendil-works" / "pi-coding-agent" / "dist" / "cli.js",
        Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / "@earendil-works" / "pi-coding-agent" / "dist" / "cli.js",
        Path("/usr/local/lib/node_modules") / "@earendil-works" / "pi-coding-agent" / "dist" / "cli.js",
    ])
    for candidate in candidates:
        if candidate.is_file():
            return node, str(candidate)
    raise RuntimeError(
        "找不到 pi 的 cli.js，请通过 npm install -g @earendil-works/pi-coding-agent 安装"
    )


@dataclass
class Task:
    name: str
    model: str
    task: str
    out_file: str
    timeout_min: int
    returncode: int | None = None
    elapsed_s: float = 0.0
    timed_out: bool = False
    error: str = ""


def run_one(task: Task, concurrency_sem: threading.Semaphore, launcher: tuple[str, str]) -> None:
    with concurrency_sem:
        log_path = LOG_DIR / f"{task.name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        node, cli_js = launcher
        cmd = [
            node, cli_js,
            "-p", "--mode", "json", "--no-session",
            "--model", task.model,
            "--thinking", THINKING,
            task.task,
        ]
        started = time.monotonic()
        try:
            with open(log_path, "w", encoding="utf-8") as handle:
                proc = subprocess.run(
                    cmd,
                    cwd=str(REPO_ROOT),
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    timeout=task.timeout_min * 60,
                )
            task.returncode = proc.returncode
        except subprocess.TimeoutExpired:
            task.timed_out = True
            task.returncode = -1
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(f"\n[TIMEOUT after {task.timeout_min} min]\n")
        except OSError as exc:
            task.returncode = -2
            task.error = str(exc)
        task.elapsed_s = time.monotonic() - started


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", required=True, help="任务定义 JSON 文件路径")
    parser.add_argument("--concurrency", type=int, default=3, help="并行度（默认 3）")
    parser.add_argument("--timeout-min", type=int, default=30, help="单任务超时分钟（默认 30）")
    args = parser.parse_args()

    tasks_path = Path(args.tasks)
    if not tasks_path.is_absolute():
        tasks_path = REPO_ROOT / tasks_path
    try:
        raw = json.loads(tasks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"任务文件读取失败: {exc}", file=sys.stderr)
        return 1
    if not isinstance(raw, list) or not raw:
        print("tasks 必须是非空数组", file=sys.stderr)
        return 1

    tasks = []
    for item in raw:
        task_text = item.get("task")
        task_file = item.get("task_file")
        if task_file:
            task_path = Path(task_file)
            if not task_path.is_absolute():
                task_path = REPO_ROOT / task_path
            try:
                task_text = task_path.read_text(encoding="utf-8")
            except OSError as exc:
                print(f"任务文件读取失败 {task_file}: {exc}", file=sys.stderr)
                return 1
        if not task_text:
            print(f"任务 {item.get('name')} 缺少 task 或 task_file", file=sys.stderr)
            return 1
        tasks.append(
            Task(
                name=str(item["name"]),
                model=str(item.get("model") or PI_MODEL_DEFAULT),
                task=str(task_text),
                out_file=str(item.get("out_file") or f"docs/review/{item['name']}.md"),
                timeout_min=int(item.get("timeout_min") or args.timeout_min),
            )
        )

    print(f"启动 {len(tasks)} 个子代理（并发 {args.concurrency}，模型默认 {PI_MODEL_DEFAULT}）")
    try:
        launcher = _find_pi_launcher()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"pi 启动器: node={launcher[0]}\ncli.js={launcher[1]}")
    sem = threading.Semaphore(args.concurrency)
    threads = [
        threading.Thread(target=run_one, args=(task, sem, launcher), name=task.name, daemon=True)
        for task in tasks
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    print("\n===== 子代理执行汇总 =====")
    ok = True
    for task in tasks:
        status = "OK" if task.returncode == 0 else "FAIL"
        if task.returncode != 0:
            ok = False
        extra = "TIMEOUT" if task.timed_out else ("ERROR" if task.returncode == -2 else "")
        report = REPO_ROOT / task.out_file
        report_state = "报告已生成" if report.exists() else "报告缺失"
        print(
            f"[{status}] {task.name:<16} exit={task.returncode} "
            f"{task.elapsed_s:6.1f}s {extra} {report_state} -> {task.out_file}"
        )
        if task.error:
            print(f"         错误: {task.error}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
