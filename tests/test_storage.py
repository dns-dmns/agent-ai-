"""Atomic writes and lock-protected read-modify-write in app/storage.py."""
import json
import os
import tempfile
import threading
from pathlib import Path

os.environ.setdefault("QA_WORKSPACE", tempfile.mkdtemp(prefix="qa_storage_"))

from app import storage  # noqa: E402


def test_write_then_read_round_trips(tmp_path: Path):
    target = tmp_path / "nested" / "doc.json"
    storage.write_json(target, {"a": 1, "b": [1, 2]})
    assert storage.read_json(target) == {"a": 1, "b": [1, 2]}
    # indent=4 matches what existing files on disk already use
    assert target.read_text(encoding="utf-8").startswith("{\n    ")


def test_missing_and_corrupt_files_return_default(tmp_path: Path):
    assert storage.read_json(tmp_path / "nope.json", []) == []
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert storage.read_json(bad, {"x": 0}) == {"x": 0}


def test_failed_write_leaves_original_and_no_temp_file(tmp_path: Path):
    target = tmp_path / "keep.json"
    storage.write_json(target, {"v": 1})

    class Unserialisable:
        pass

    try:
        storage.write_json(target, {"v": Unserialisable()})
    except TypeError:
        pass
    assert json.loads(target.read_text(encoding="utf-8")) == {"v": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["keep.json"]


def test_update_json_does_not_write_when_block_raises(tmp_path: Path):
    target = tmp_path / "list.json"
    storage.write_json(target, [1])
    try:
        with storage.update_json(target, list) as doc:
            doc.data.append(2)
            raise RuntimeError("abort")
    except RuntimeError:
        pass
    assert storage.read_json(target) == [1]


def test_concurrent_updates_lose_nothing(tmp_path: Path):
    """The run_history/bugs race: N writers appending at once must all land."""
    target = tmp_path / "history.json"
    writers, per_writer = 8, 25

    def worker(n: int) -> None:
        for i in range(per_writer):
            with storage.update_json(target, list) as doc:
                doc.data.append(f"{n}-{i}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = storage.read_json(target)
    assert len(rows) == writers * per_writer
    assert len(set(rows)) == writers * per_writer


def test_learned_profile_survives_parallel_learning(tmp_path: Path, monkeypatch):
    from app import learned

    monkeypatch.setattr(learned, "path_for", lambda project: tmp_path / f"{project}.json")

    def worker(n: int) -> None:
        for i in range(10):
            learned.remember_healed("p", "Flow", f"#old{n}_{i}", f"#new{n}_{i}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    healed = learned.load("p")["flows"]["Flow"]["healed"]
    assert len(healed) == 60
