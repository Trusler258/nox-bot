"""KV 存储（§8.4）：data/kv/<ns>.json，一个命名空间一个文件。"""
import json

from kernel.storage.kv import KV


def test_set_get_roundtrip(tmp_path):
    kv = KV(tmp_path)
    kv.set("wdsj", "default_player", "trusler")
    assert kv.get("wdsj", "default_player") == "trusler"
    assert kv.get("wdsj", "missing", "x") == "x"

    # 落盘的就是 data/kv/<ns>.json，人能直接看懂
    raw = json.loads((tmp_path / "wdsj.json").read_text(encoding="utf-8"))
    assert raw == {"default_player": "trusler"}


def test_namespaces_are_isolated(tmp_path):
    kv = KV(tmp_path)
    kv.set("a", "k", 1)
    kv.set("b", "k", 2)
    assert kv.get("a", "k") == 1
    assert kv.get("b", "k") == 2
    assert kv.namespaces() == ["a", "b"]


def test_delete_and_clear(tmp_path):
    kv = KV(tmp_path)
    kv.update("ns", {"a": 1, "b": 2})
    assert kv.delete("ns", "a") is True
    assert kv.delete("ns", "a") is False
    assert kv.all("ns") == {"b": 2}
    assert kv.clear("ns") == 1
    assert kv.all("ns") == {}


def test_bad_json_is_backed_up_not_silently_reset(tmp_path):
    f = tmp_path / "broken.json"
    f.write_text("{not json", encoding="utf-8")
    kv = KV(tmp_path)
    assert kv.all("broken") == {}
    assert (tmp_path / "broken.json.bad").is_file()


def test_external_change_is_picked_up(tmp_path):
    """别的进程改了文件，按 mtime 失效缓存后能读到新值。"""
    kv = KV(tmp_path)
    kv.set("ns", "k", "old")
    f = tmp_path / "ns.json"
    f.write_text(json.dumps({"k": "new"}), encoding="utf-8")
    import os
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 5))
    assert kv.get("ns", "k") == "new"


def test_namespace_name_is_sanitized(tmp_path):
    kv = KV(tmp_path)
    kv.set("../../etc/passwd", "k", 1)
    assert kv.get("../../etc/passwd", "k") == 1
    # 不能真的写到上级目录
    assert list(tmp_path.glob("*.json"))
    assert not (tmp_path.parent / "etc").exists()
