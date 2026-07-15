from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from pi_manager import core
from pi_manager import extras
from pi_manager import storage


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
