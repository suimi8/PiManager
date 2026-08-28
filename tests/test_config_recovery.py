"""配置损坏后的可恢复性（审查 P0-1 / P0-2 / P3-7）。

基线（HEAD=4b5a4fc）上的实测症状：

- P0-1  ``restore_config_backup`` -> ``{'ok': False, 'error': '恢复失败：拒绝覆盖
        无法读取的配置文件…'}``：唯一的修复入口被 ``storage._write_unlocked``
        的防误覆盖守卫挡住，应用内没有「删除损坏文件」的出口 → 永久只读。
- P0-2  ``settings.json`` / ``pi-manager.json`` 损坏 → ``get_theme()`` /
        ``get_language()`` / ``get_ui_theme()`` 直接抛 ``CorruptJsonError``，
        而这些是启动路径函数、没有 try/except → 应用起不来。
        ``models.json`` 虽然有备份兜底，但只兜到内存：损坏文件仍在原地，
        之后每一次写入照样被守卫拒绝。

这些用例全部使用 ``isolated_home``：项目出过测试污染开发者真实
``~/.pi/agent/`` 的事故，任何写配置的用例都不得例外。
"""
from __future__ import annotations

import json

import pytest

from pi_manager import core, storage

CORRUPT_JSON = '{"ui_mode": "dark", TRUNCATED'


def _corrupt(path, text: str = CORRUPT_JSON) -> None:
    path.write_text(text, encoding="utf-8")
    core._invalidate_config_cache(None)


# ---------------------------------------------------------------- P0-1


def test_restore_config_backup_repairs_a_corrupt_target(isolated_home):
    """P0-1：目标损坏时「备份恢复」必须能真正恢复。"""
    target = core.settings_path()
    core.save_json(target, {"ui_mode": "dark"})          # 写第一版
    core.save_json(target, {"ui_mode": "dark", "x": 1})  # 轮转出 .bak.1
    backup = core.pi_agent_dir() / "settings.json.bak.1"
    assert json.loads(backup.read_text(encoding="utf-8")) == {"ui_mode": "dark"}

    _corrupt(target)
    assert core.load_json(backup, None) == {"ui_mode": "dark"}  # 备份本身完好

    result = core.restore_config_backup(backup)

    assert result["ok"] is True, result
    assert result["target"] == "settings.json"
    assert json.loads(target.read_text(encoding="utf-8")) == {"ui_mode": "dark"}
    # 恢复后写入不再被守卫拒绝（不然只是把死角推后一步）
    core.save_json(target, {"ui_mode": "light"})
    assert core.load_settings()["ui_mode"] == "light"


def test_restore_quarantines_corrupt_content_and_keeps_backup_chain(isolated_home):
    """P0-1 取证 + P3-7 备份链：损坏内容另存，备份不被轮转挤掉。"""
    root = core.pi_agent_dir()
    target = core.settings_path()
    core.save_json(target, {"v": 1})
    core.save_json(target, {"v": 2})  # .bak.1 = {"v": 1}
    core.save_json(target, {"v": 3})  # .bak.1 = {"v": 2}, .bak.2 = {"v": 1}
    before = {
        p.name: p.read_text(encoding="utf-8") for p in root.glob("settings.json.bak.*")
    }
    assert set(before) == {"settings.json.bak.1", "settings.json.bak.2"}

    _corrupt(target)
    assert core.restore_config_backup(root / "settings.json.bak.2")["ok"] is True

    # 备份链一字未动：连续恢复不会把仅存的可用备份挤掉
    after = {
        p.name: p.read_text(encoding="utf-8") for p in root.glob("settings.json.bak.*")
    }
    assert after == before
    # 损坏内容被隔离，且不会被误认成可恢复备份
    quarantined = sorted(root.glob("settings.json.corrupt.*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == CORRUPT_JSON
    assert all(
        row["name"] != quarantined[0].name for row in core.list_config_backups()
    )


def test_plain_save_still_refuses_to_overwrite_corrupt_files(isolated_home):
    """绕过必须窄：普通写入路径的防误覆盖守卫不得被削弱。"""
    target = core.settings_path()
    core.save_json(target, {"v": 1})
    _corrupt(target)

    with pytest.raises(storage.CorruptJsonError):
        storage.save_json(target, {"v": 2})
    with pytest.raises(storage.CorruptJsonError):
        storage.update_json(target, {}, lambda cur: {"v": 3})
    # 损坏内容仍在原地，没有被静默覆盖，也没有被无端隔离
    assert target.read_text(encoding="utf-8") == CORRUPT_JSON
    assert not list(core.pi_agent_dir().glob("settings.json.corrupt.*"))


def test_restore_rejects_paths_outside_the_agent_dir(isolated_home, tmp_path):
    """恢复入口新增了 allow_corrupt_overwrite，路径白名单必须依然生效。"""
    outsider = tmp_path / "settings.json.bak.1"
    outsider.write_text('{"v": 1}', encoding="utf-8")
    assert core.restore_config_backup(outsider)["ok"] is False

    bogus = core.pi_agent_dir() / "evil.json.bak.1"
    bogus.parent.mkdir(parents=True, exist_ok=True)
    bogus.write_text('{"v": 1}', encoding="utf-8")
    assert core.restore_config_backup(bogus)["ok"] is False


# ---------------------------------------------------------------- P0-2


@pytest.mark.parametrize(
    "path_factory, reader, expected",
    [
        (core.settings_path, core.get_theme, "dark"),
        (core.manager_config_path, core.get_language, "zh-CN"),
        (core.models_path, lambda: core.load_models_config()["providers"], {}),
    ],
)
def test_startup_readers_survive_a_corrupt_config(
    isolated_home, path_factory, reader, expected
):
    """P0-2：三份配置任一损坏都不能让启动路径抛异常。"""
    path = path_factory()
    path.parent.mkdir(parents=True, exist_ok=True)
    _corrupt(path, "{BROKEN")

    assert reader() == expected  # 无备份 → 回退默认值，不抛异常


def test_corrupt_config_falls_back_to_the_newest_backup(isolated_home):
    """P0-2：有备份时优先用备份，而不是直接退默认值。"""
    core.save_manager_config({"language": "en", "ui_accent": "green"})
    core.save_manager_config({"language": "en", "ui_accent": "purple"})
    _corrupt(core.manager_config_path(), "{BROKEN")

    assert core.get_language() == "en"
    assert core.get_ui_theme()["accent"] == "green"


def test_corrupt_config_is_repaired_on_disk_not_only_in_memory(isolated_home):
    """P0-2 的另一半：只在内存兜底等于把「永久只读」推后一步。

    基线上 models.json 有备份兜底，但损坏文件留在原地 → 之后每次
    ``save_models_config`` 仍被守卫拒绝。修复后读取即修盘。
    """
    for path, load, save in (
        (core.settings_path(), core.load_settings, core.save_settings),
        (core.manager_config_path(), core.load_manager_config, core.save_manager_config),
        (core.models_path(), core.load_models_config, core.save_models_config),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        _corrupt(path, "{BROKEN")

        load()  # 触发修复

        # 文件已经可解析，损坏内容留证
        assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict)
        assert list(path.parent.glob(f"{path.name}.corrupt.*"))
        # 而且真的能写了
        data = load()
        data["_probe"] = "written"
        save(data)


def test_startup_path_functions_do_not_raise_on_any_corrupt_config(isolated_home):
    """启动路径的整体冒烟：三份配置同时损坏也要能起来。"""
    for path in (core.settings_path(), core.manager_config_path(), core.models_path()):
        path.parent.mkdir(parents=True, exist_ok=True)
        _corrupt(path, "not json at all")

    assert core.get_language() in {"zh-CN", "en", "auto"}
    assert core.get_ui_theme()["mode"] in {"day", "night"}
    assert core.is_setup_done() is False
    assert core.get_default_model()[:2] == ("", "")
    assert core.get_theme()
    assert core.get_provider_config("nope") is None


def test_manager_defaults_never_capture_the_real_home(isolated_home):
    """``last_workdir`` 的默认值必须在调用时求值，否则会钉死真实 HOME。"""
    assert core.load_manager_config()["last_workdir"] == str(isolated_home)


# ---------------------------------------------------------------- P3-2


def test_internal_return_channel_keys_are_never_persisted(isolated_home):
    """``_purge`` / ``_purged_enabled`` 不得落进 models.json。"""
    core.upsert_custom_provider(
        "Alpha", base_url="https://a.example/v1", api_key="sk-a", models=[{"id": "m1"}]
    )
    result = core.delete_custom_provider("Alpha")
    assert "_purge" in result  # UI 依赖这个返回契约

    core.save_models_config(result)  # 有人这么写也不该污染配置
    on_disk = json.loads(core.models_path().read_text(encoding="utf-8"))
    assert not [key for key in on_disk if str(key).startswith("_")]
