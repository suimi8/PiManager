from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pi_manager import core
from pi_manager import extras
from pi_manager import storage

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_child(script: str, tmp_path: Path, home: Path | None, *args: str):
    """在**真子进程**里跑一段脚本。

    历史上这个文件的并发用例全是同进程 ThreadPool —— 那只能验证
    ``threading.RLock``，``msvcrt.locking`` / ``fcntl.flock`` 从未真正生效过。
    跨进程锁是 storage 的核心承诺（Cursor 扩展调起的 helper 进程、
    ``main.py --config-mutate`` 都在写同一批配置），必须用真进程验证。
    """
    path = tmp_path / "child.py"
    path.write_text(script, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_REPO_ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    if home is not None:
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
    return subprocess.Popen(
        [sys.executable, str(path), *args],
        env=env,
        cwd=str(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )


def test_atomic_json_updates_do_not_drop_records(tmp_path):
    path = tmp_path / "records.json"

    def append(value: int):
        storage.update_json(path, [], lambda rows: [*rows, value])

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(append, range(100)))
    rows = storage.load_json(path, [])
    assert len(rows) == 100
    assert set(rows) == set(range(100))


def test_concurrent_history_appends_are_complete(isolated_home):
    def append(value: int):
        extras.append_test_history(
            [
                {
                    "provider": "p",
                    "model": f"m-{value}",
                    "available": True,
                    "latency_ms": value,
                    "mode": "mock",
                }
            ]
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(append, range(100)))
    history = extras.load_history()
    assert len(history) == 100
    assert {item["model"] for item in history} == {f"m-{i}" for i in range(100)}


def test_health_results_are_committed_together(isolated_home, monkeypatch):
    pairs = [("p", f"m-{i}") for i in range(32)]

    def fake_batch(received, **kwargs):
        assert received == pairs
        return [
            {
                "provider": provider,
                "model": model,
                "available": True,
                "latency_ms": index,
                "mode": "mock",
            }
            for index, (provider, model) in enumerate(received)
        ]

    monkeypatch.setattr(extras, "test_models_batch_concurrent", fake_batch)
    result = extras.run_health_check(pairs=pairs, scope="selected")
    assert result["ok"] is True
    assert len(result["health"]["models"]) == 32
    assert len(core.load_json(extras.health_path(), {})["models"]) == 32


def test_config_cache_invalidates_on_write_and_copies_are_isolated(isolated_home):
    from pi_manager import core

    mgr = core.load_manager_config()
    mgr["proxy_enabled"] = False
    core.save_manager_config(mgr)
    assert core.load_manager_config()["proxy_enabled"] is False

    mgr = core.load_manager_config()
    mgr["proxy_enabled"] = True
    core.save_manager_config(mgr)
    assert core.load_manager_config()["proxy_enabled"] is True

    # Mutating a returned copy must never leak into later reads.
    leaked = core.load_manager_config()
    leaked["proxy_enabled"] = "mutated"
    leaked["favorites"].append("Evil/model")
    fresh = core.load_manager_config()
    assert fresh["proxy_enabled"] is True
    assert "Evil/model" not in fresh["favorites"]


# ===================== P1-1　locked() 可重入 =====================
#
# 基线（HEAD=4b5a4fc）实测：
#   嵌套 locked() 同路径同线程        -> OSError: [Errno 36] Resource deadlock avoided (9.09s)
#   update_json + updater 内 load_json -> 同上 (9.09s)
# 线程锁是 RLock（放行嵌套），但随后打开的是**新的文件句柄**，
# msvcrt.locking / fcntl.flock 的锁归属于句柄，同进程不同句柄依然互斥。


def test_nested_locked_same_path_same_thread_is_reentrant(tmp_path):
    path = tmp_path / "nested.json"
    started = time.monotonic()
    with storage.locked(path):
        with storage.locked(path):
            with storage.locked(path):
                pass
    # 基线在这里要么抛 OSError（Windows，约 9s），要么永久阻塞（POSIX flock）。
    assert time.monotonic() - started < 5.0


def test_reentrant_lock_key_is_normalized(tmp_path):
    """不同写法的同一文件必须命中同一个重入计数 key，否则又会自锁。"""
    path = tmp_path / "sub" / "cfg.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    alias = tmp_path / "sub" / ".." / "sub" / "cfg.json"
    cased = Path(str(path).upper()) if os.name == "nt" else path
    started = time.monotonic()
    with storage.locked(path):
        with storage.locked(alias):
            with storage.locked(cased):
                pass
    assert time.monotonic() - started < 5.0


def test_read_modify_write_can_now_be_wrapped_in_one_lock(tmp_path):
    """P1-1 的目的：让上层能把读-改-写整体持锁（P1-2 的修复落脚点）。"""
    path = tmp_path / "rmw.json"
    storage.save_json(path, {"n": 0})
    with storage.locked(path):
        current = storage.load_json(path, {})
        storage.save_json(path, {"n": current["n"] + 1})
    assert storage.load_json(path, {}) == {"n": 1}

    # updater 内部再读一次同一文件（extras 里被迫放弃的写法）
    storage.update_json(
        path, {}, lambda cur: {"n": storage.load_json(path, {})["n"] + 10}
    )
    assert storage.load_json(path, {}) == {"n": 11}


def test_reentry_counter_is_released_on_exception(tmp_path):
    """异常路径也必须把计数退干净，否则后续加锁会跳过 OS 锁（静默失去互斥）。"""
    path = tmp_path / "boom.json"
    key = storage._lock_key(path)
    with pytest.raises(RuntimeError):
        with storage.locked(path):
            with storage.locked(path):
                raise RuntimeError("boom")
    assert storage._held_depths().get(key, 0) == 0
    with storage.locked(path):
        assert storage._held_depths()[key] == 1
    assert key not in storage._held_depths()


def test_reentrancy_does_not_let_other_threads_in(tmp_path):
    """可重入只对「同一线程」放行；另一个线程必须真正等待。"""
    path = tmp_path / "threads.json"
    order: list[str] = []
    other_started = threading.Event()

    def other() -> None:
        other_started.set()
        with storage.locked(path):
            order.append("other-acquired")

    with storage.locked(path):
        worker = threading.Thread(target=other, daemon=True)
        worker.start()
        other_started.wait(5)
        with storage.locked(path):
            order.append("inner-enter")
        order.append("inner-exit")
        # 内层退出后仍然持锁：另一个线程此刻绝不能已经进去
        time.sleep(0.3)
        assert "other-acquired" not in order
        order.append("outer-exit")
    worker.join(15)
    assert order == ["inner-enter", "inner-exit", "outer-exit", "other-acquired"]


# ===================== P1-1　跨进程语义必须保持 =====================

_CHILD_WAIT = '''
import sys, time
from pathlib import Path
from pi_manager import storage

target = Path(sys.argv[1])
Path(sys.argv[2]).write_text("ready", encoding="utf-8")
started = time.monotonic()
with storage.locked(target):
    print("WAITED=%.3f" % (time.monotonic() - started), flush=True)
'''


@pytest.mark.parametrize("nested", [False, True])
def test_inter_process_lock_still_blocks_another_process(tmp_path, nested):
    """重入计数是 per-process 的：其它进程仍必须被 OS 锁挡住。

    ``nested=True`` 是本次改造最关键的回归护栏：内层 ``locked()`` 退出时**不能**
    释放 OS 锁，否则子进程会在父进程还在临界区里时抢到锁。
    """
    target = tmp_path / "xproc.json"
    target.write_text("{}", encoding="utf-8")
    ready = tmp_path / "ready.flag"
    hold = 1.6

    with storage.locked(target):
        proc = _run_child(_CHILD_WAIT, tmp_path, None, str(target), str(ready))
        deadline = time.monotonic() + 30
        while not ready.exists() and time.monotonic() < deadline:
            assert proc.poll() is None, "子进程提前退出"
            time.sleep(0.05)
        assert ready.exists(), "子进程未就绪"
        if nested:
            with storage.locked(target):
                time.sleep(hold / 2)
            time.sleep(hold / 2)
        else:
            time.sleep(hold)
        assert proc.poll() is None, "子进程在父进程持锁期间就拿到了锁"
    out, _ = proc.communicate(timeout=90)
    assert "WAITED=" in out, out
    waited = float(out.split("WAITED=")[1].split()[0])
    assert waited >= hold * 0.5, f"子进程只等了 {waited}s，跨进程互斥已被破坏：{out}"


_CHILD_APPEND = '''
import sys
from pathlib import Path
from pi_manager import storage

target = Path(sys.argv[1])
tag = sys.argv[2]
count = int(sys.argv[3])
for index in range(count):
    label = "%s-%d" % (tag, index)
    storage.update_json(target, [], lambda rows, label=label: [*(rows or []), label])
print("DONE", flush=True)
'''


def test_update_json_loses_nothing_across_real_processes(tmp_path):
    """两个真进程各自 update_json，不能丢任何一条（跨进程锁真的在工作）。"""
    target = tmp_path / "shared.json"
    storage.save_json(target, [])
    per_side = 8

    proc = _run_child(
        _CHILD_APPEND, tmp_path, None, str(target), "child", str(per_side)
    )
    for index in range(per_side):
        label = f"parent-{index}"
        storage.update_json(
            target, [], lambda rows, label=label: [*(rows or []), label]
        )
    out, _ = proc.communicate(timeout=180)
    assert proc.returncode == 0, out

    rows = storage.load_json(target, [])
    assert len(rows) == per_side * 2, out
    expected = {f"parent-{i}" for i in range(per_side)}
    expected |= {f"child-{i}" for i in range(per_side)}
    assert set(rows) == expected


# ===================== P1-2　无锁读-改-写导致的丢失更新 =====================
#
# 基线实测：12 轮「同时改主题 + 改语言」中 12 轮丢失更新
# （最终 pi-manager.json 里 ui_mode 或 language 被整份覆盖回退）。


def test_concurrent_manager_config_writers_do_not_lose_updates(isolated_home):
    for _ in range(12):
        core.save_manager_config({"ui_mode": "night", "language": "zh-CN"})
        core._invalidate_config_cache(None)
        barrier = threading.Barrier(2)

        def change_theme() -> None:
            barrier.wait(20)
            core.set_ui_theme(mode="day")

        def change_language() -> None:
            barrier.wait(20)
            core.set_language("en")

        with ThreadPoolExecutor(max_workers=2) as pool:
            for future in [pool.submit(change_theme), pool.submit(change_language)]:
                future.result(timeout=60)

        core._invalidate_config_cache(None)
        final = json.loads(core.manager_config_path().read_text(encoding="utf-8"))
        assert final["ui_mode"] == "day", final
        assert final["language"] == "en", final


def test_concurrent_settings_writers_do_not_lose_updates(isolated_home):
    core.save_settings({})
    barrier = threading.Barrier(3)

    def set_default() -> None:
        barrier.wait(20)
        core.set_default_model("P", "m", "high")

    def set_enabled() -> None:
        barrier.wait(20)
        core.set_enabled_models(["P/m"])

    def set_theme() -> None:
        barrier.wait(20)
        core.apply_theme("light")

    with ThreadPoolExecutor(max_workers=3) as pool:
        for future in [
            pool.submit(set_default),
            pool.submit(set_enabled),
            pool.submit(set_theme),
        ]:
            future.result(timeout=60)

    core._invalidate_config_cache(None)
    settings = json.loads(core.settings_path().read_text(encoding="utf-8"))
    assert settings["defaultProvider"] == "P"
    assert settings["enabledModels"] == ["P/m"]
    assert settings["theme"] == "light"


def test_concurrent_provider_model_writers_do_not_lose_updates(isolated_home):
    core.upsert_custom_provider(
        "Alpha", base_url="https://a.example/v1", api_key="sk-a", models=[]
    )
    barrier = threading.Barrier(6)

    def add(index: int) -> None:
        barrier.wait(30)
        core.add_model_to_provider("Alpha", f"m{index}")

    with ThreadPoolExecutor(max_workers=6) as pool:
        for future in [pool.submit(add, i) for i in range(6)]:
            future.result(timeout=60)

    entry = core.get_provider_config("Alpha")
    assert entry is not None
    assert {m["id"] for m in entry["models"]} == {f"m{i}" for i in range(6)}


def test_manager_and_settings_writers_do_not_deadlock(isolated_home):
    """``set_ui_theme`` 先写 pi-manager.json 再写 settings.json。

    两把锁**不得嵌套**，否则与「先 settings 后 manager」的调用方构成 ABBA 死锁。
    """
    core.save_manager_config({"ui_mode": "night"})
    barrier = threading.Barrier(4)

    def flip_ui(mode: str) -> None:
        barrier.wait(30)
        core.set_ui_theme(mode=mode)

    # 刻意不用 apply_theme 作为「纯 settings 写入方」：它会先调
    # builtin_themes.ensure_builtin_themes()，而那里的临时文件名只含 PID 不含
    # 线程 ID（审查 P3-9），并发调用会自己撞车 —— 与本用例要验证的锁序无关。
    def flip_default(model: str) -> None:
        barrier.wait(30)
        core.set_default_model("P", model)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(flip_ui, "day"),
            pool.submit(flip_ui, "night"),
            pool.submit(flip_default, "m1"),
            pool.submit(flip_default, "m2"),
        ]
        for future in futures:
            future.result(timeout=60)

    assert core.get_ui_theme()["mode"] in {"day", "night"}
    assert core.get_default_model()[1] in {"m1", "m2"}


# ===================== 空写不得挤掉备份历史 =====================


def test_unchanged_updates_skip_the_write_and_keep_backups(isolated_home):
    core.save_settings({"theme": "dark"})
    core.save_settings({"theme": "dark", "v": 2})
    backups = {
        p.name: p.read_text(encoding="utf-8")
        for p in core.pi_agent_dir().glob("settings.json.bak.*")
    }
    mtime = core.settings_path().stat().st_mtime_ns

    core.sync_cli_theme_with_ui("night")  # theme 已经是 dark → 无需改动

    assert core.settings_path().stat().st_mtime_ns == mtime
    assert {
        p.name: p.read_text(encoding="utf-8")
        for p in core.pi_agent_dir().glob("settings.json.bak.*")
    } == backups


# ===================== P2-8　AGENTS.md 原子写 =====================


def test_agents_md_is_written_atomically_with_a_backup(isolated_home):
    core.ensure_agent_dir()
    path = core.agents_md_path()
    path.write_text("# 我的全局指令\n\n手写内容必须留住。\n", encoding="utf-8")

    core.apply_language_preference("en")
    first = path.read_text(encoding="utf-8")
    assert "手写内容必须留住" in first
    assert "PI-MANAGER-LANG-START" in first

    core.apply_language_preference("zh-CN")
    second = path.read_text(encoding="utf-8")
    assert "手写内容必须留住" in second
    assert second.count("PI-MANAGER-LANG-START") == 1
    # 原子写顺带留下备份，出错还有退路（基线的裸 write_text 没有）
    assert (path.parent / f"{path.name}.bak.1").exists()
    # 没有残留的临时文件
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))


# ===================== P2-6　models.json 迁移的幂等性与写放大 =====================

_LEGACY_MODELS = {
    "providers": {
        "Legacy": {
            "baseUrl": "https://legacy.example/v1",
            "api": "openai-completions",
            "apiKey": "!LEGACY_ENV_REF",
            "models": [{"id": "r1", "reasoning": True}],
        }
    }
}


def test_models_migrations_are_idempotent(isolated_home):
    """m(m(x)) == m(x)：三轮迁移的幂等性以前只是隐式前提，没有任何守护。

    一旦某轮不幂等，``load_models_config`` 会每次调用都写一次盘（写放大 + 缓存
    永久未命中），而它是热路径。
    """
    once, changed_once = core._migrate_models_config(_LEGACY_MODELS)
    assert changed_once is True
    twice, changed_twice = core._migrate_models_config(once)
    assert changed_twice is False
    assert twice == once


def test_load_models_config_does_not_rewrite_on_every_call(isolated_home):
    """迁移完成后重复读取不得再写盘。"""
    storage.save_json(core.models_path(), _LEGACY_MODELS)
    core._invalidate_config_cache(None)
    core.load_models_config()  # 触发一次迁移落盘

    mtime = core.models_path().stat().st_mtime_ns
    for _ in range(5):
        core._invalidate_config_cache(None)
        core.load_models_config()
    assert core.models_path().stat().st_mtime_ns == mtime


def test_models_migration_does_not_revert_a_concurrent_write(isolated_home, monkeypatch):
    """迁移落盘必须在锁内基于磁盘最新内容重算，否则会整份覆盖并发写入。"""
    storage.save_json(core.models_path(), _LEGACY_MODELS)
    core._invalidate_config_cache(None)

    original = core._migrate_models_headers
    calls: list[int] = []

    def racing_migrate(cfg):
        calls.append(1)
        if len(calls) == 1:
            # 第一次调用是「锁外的内存检测」。在拿锁之前插入一个并发写入，
            # 模拟另一个线程/进程刚加了一个 Provider。
            disk = storage.load_json(core.models_path(), {})
            disk["providers"]["Rival"] = {
                "baseUrl": "https://rival.example/v1",
                "api": "anthropic",
                "models": [],
            }
            storage.save_json(core.models_path(), disk)
            core._invalidate_config_cache(None)
        return original(cfg)

    monkeypatch.setattr(core, "_migrate_models_headers", racing_migrate)
    monkeypatch.setattr(
        core,
        "_MODELS_MIGRATIONS",
        (core._migrate_models_keys, racing_migrate, core._migrate_models_thinking),
    )
    core.load_models_config()

    on_disk = json.loads(core.models_path().read_text(encoding="utf-8"))
    assert "Rival" in on_disk["providers"], "并发写入被迁移整份覆盖回退了"
    assert "Legacy" in on_disk["providers"]
    # 迁移结果本身也要在
    assert on_disk["providers"]["Legacy"]["models"][0]["thinkingLevelMap"]
