from __future__ import annotations

from pi_manager import core


def test_delete_provider_purges_favorites_and_redefaults(isolated_home):
    core.upsert_custom_provider(
        "Alpha",
        base_url="https://a.example/v1",
        api_key="sk-a",
        models=[{"id": "m1"}, {"id": "m2"}],
    )
    core.upsert_custom_provider(
        "Beta",
        base_url="https://b.example/v1",
        api_key="sk-b",
        models=[{"id": "m3"}],
    )
    mgr = core.load_manager_config()
    mgr["favorites"] = ["Alpha/m1", "Alpha/m2", "Beta/m3"]
    core.save_manager_config(mgr)
    core.set_default_model("Alpha", "m1", "medium")

    result = core.delete_custom_provider("Alpha")
    purge = result.get("_purge") or {}

    assert "Alpha/m1" in purge.get("removed_favorites", [])
    assert "Alpha/m2" in purge.get("removed_favorites", [])
    assert core.load_manager_config().get("favorites") == ["Beta/m3"]
    assert purge.get("default_changed") is True
    assert purge.get("default_provider") == "Beta"
    assert purge.get("default_model") == "m3"
    assert core.get_default_model()[:2] == ("Beta", "m3")
    assert core.get_provider_config("Alpha") is None


def test_purge_favorite_model_redefaults_to_next(isolated_home):
    mgr = core.load_manager_config()
    mgr["favorites"] = ["P/a", "P/b", "Q/c"]
    core.save_manager_config(mgr)
    core.set_default_model("P", "a")

    purge = core.purge_favorites(provider="P", model="a", redefault=True)
    assert purge["removed_favorites"] == ["P/a"]
    assert core.load_manager_config()["favorites"] == ["P/b", "Q/c"]
    assert purge["default_changed"] is True
    assert (purge["default_provider"], purge["default_model"]) == ("P", "b")


def test_purge_last_favorite_clears_default(isolated_home):
    mgr = core.load_manager_config()
    mgr["favorites"] = ["Only/x"]
    core.save_manager_config(mgr)
    core.set_default_model("Only", "x")

    purge = core.purge_favorites(provider="Only", redefault=True)
    assert purge["removed_favorites"] == ["Only/x"]
    assert core.load_manager_config()["favorites"] == []
    assert purge["default_changed"] is True
    assert core.get_default_model()[:2] == ("", "")
