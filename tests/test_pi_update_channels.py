from __future__ import annotations

from types import SimpleNamespace

from pi_manager import core


def _runtime(*, installed: str | None = None, missing: bool = False, broken: bool = False):
    return {
        "command": None if missing else "C:/tools/pi.cmd",
        "installed": installed,
        "raw_version": installed,
        "missing": missing,
        "runtime_broken": broken,
        "ok": bool(installed) and not broken,
        "error": "Pi requires Node.js >=22.19.0" if broken else "",
    }


def test_pi_runtime_error_does_not_parse_node_version_as_pi(monkeypatch):
    monkeypatch.setattr(
        core,
        "run_pi",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Pi requires Node.js >=22.19.0\nCurrent version: 20.18.0\n",
        ),
    )
    monkeypatch.setattr(core, "find_pi_command", lambda: "C:/tools/pi.cmd")

    raw = core.get_pi_version()
    status = core.get_pi_runtime_status()

    assert raw.startswith("error:")
    assert core.get_installed_pi_version() is None
    assert status["installed"] is None
    assert status["runtime_broken"] is True
    assert status["ok"] is False


def test_node_22_19_or_newer_uses_latest_channel():
    assert core.select_pi_install_channel("22.19.0") == core.PI_LATEST_TAG
    assert core.select_pi_install_channel("24.1.0") == core.PI_LATEST_TAG


def test_node_20_compatibility_range_uses_legacy_channel():
    assert core.select_pi_install_channel("20.6.0") == core.PI_LEGACY_NODE20_TAG
    assert core.select_pi_install_channel("22.18.9") == core.PI_LEGACY_NODE20_TAG


def test_node_below_20_6_is_blocked(monkeypatch):
    monkeypatch.setattr(core, "get_node_version", lambda timeout=20: "20.5.9")
    monkeypatch.setattr(core, "get_npm_version", lambda timeout=20: "10.9.0")
    monkeypatch.setattr(core, "get_pi_runtime_status", lambda: _runtime(missing=True))
    monkeypatch.setattr(
        core,
        "get_latest_pi_version",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("registry must not be queried")),
    )

    status = core.needs_pi_install_or_update()

    assert status["blocked"] is True
    assert status["installable"] is False
    assert status["ok"] is False
    assert status["channel"] is None
    assert status["latest"] is None


def test_registry_failure_is_not_reported_as_ready_and_install_is_not_run(monkeypatch):
    monkeypatch.setattr(core, "get_node_version", lambda timeout=20: "22.20.0")
    monkeypatch.setattr(core, "get_npm_version", lambda timeout=20: "11.0.0")
    monkeypatch.setattr(core, "get_pi_runtime_status", lambda: _runtime(installed="0.80.10"))
    monkeypatch.setattr(core, "get_latest_pi_version", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        core.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("npm install must not run")),
    )

    status = core.needs_pi_install_or_update()
    code, _stdout, stderr = core.install_or_update_pi()

    assert status["registry_ok"] is False
    assert status["check_failed"] is True
    assert status["ok"] is False
    assert code == 3
    assert "registry" in stderr


def test_successful_npm_exit_still_fails_when_pi_runtime_verification_fails(monkeypatch):
    monkeypatch.setattr(core, "get_node_version", lambda timeout=20: "22.20.0")
    monkeypatch.setattr(core, "get_npm_version", lambda timeout=20: "11.0.0")
    monkeypatch.setattr(core, "get_latest_pi_version", lambda timeout=20, tag=None: "0.80.10")
    monkeypatch.setattr(
        core.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="installed\n", stderr=""),
    )
    monkeypatch.setattr(core, "get_pi_runtime_status", lambda: _runtime(broken=True))

    code, stdout, stderr = core.install_or_update_pi()

    assert code == 4
    assert "installed" in stdout
    assert "Node.js" in stderr


def test_old_pi_shim_in_path_fails_post_install_verification(monkeypatch):
    monkeypatch.setattr(core, "get_node_version", lambda timeout=20: "22.20.0")
    monkeypatch.setattr(core, "get_npm_version", lambda timeout=20: "11.0.0")
    monkeypatch.setattr(core, "get_latest_pi_version", lambda timeout=20, tag=None: "0.80.10")
    monkeypatch.setattr(
        core.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="installed\n", stderr=""),
    )
    monkeypatch.setattr(core, "get_pi_runtime_status", lambda: _runtime(installed="0.74.2"))

    code, _stdout, stderr = core.install_or_update_pi()

    assert code == 5
    assert "PATH" in stderr
    assert "0.74.2" in stderr


def test_node_20_install_uses_legacy_dist_tag(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(core, "get_node_version", lambda timeout=20: "20.18.0")
    monkeypatch.setattr(core, "get_npm_version", lambda timeout=20: "10.9.0")
    monkeypatch.setattr(core, "get_latest_pi_version", lambda timeout=20, tag=None: "0.74.2")
    monkeypatch.setattr(core, "_npm_command", lambda *args: ["npm", *args])

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="installed\n", stderr="")

    monkeypatch.setattr(core.subprocess, "run", fake_run)
    monkeypatch.setattr(core, "get_pi_runtime_status", lambda: _runtime(installed="0.74.2"))

    code, _stdout, _stderr = core.install_or_update_pi()

    assert code == 0
    assert calls == [
        ["npm", "install", "-g", "@earendil-works/pi-coding-agent@legacy-node20"]
    ]
