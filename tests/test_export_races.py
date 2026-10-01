"""导出与「撤销快照 + 两阶段 GC」并发的确定性交错（阶段屏障固定顺序）。

要固定的交错（均用 ``threading.Barrier``/``Event``，无 sleep）：

* 撤销先完成：导出必须报告快照不存在，不产包、不留保活引用；
* 导出先取得保活资格：标记、撤销、复核都在读取窗口内发生，导出仍必须
  得到完整包，且成员在读取结束前不会被 GC 删除；
* 标记先于保活资格：成员在标记时存活（快照还在），复核时被保活引用救回。
"""

from __future__ import annotations

import threading

from objstore import (
    Repository,
    SnapshotNotFoundError,
    verify_package,
)

from conftest import ScriptedHooks, assert_consistent


def _seed(repo_root, clock, n=3):
    r = Repository(repo_root, clock=clock)
    ids = [r.put(f"export-member-{i}".encode() * 50) for i in range(n)]
    sid = r.publish_snapshot(ids)
    return r, ids, sid


def test_revoke_completes_before_export_reports_missing(repo_root, clock, tmp_path):
    """撤销先于导出的成员确定事务完成 → SnapshotNotFoundError，两者用屏障会合。"""

    r_main, ids, sid = _seed(repo_root, clock)

    export_started = threading.Event()
    revoke_done = threading.Event()

    hooks = ScriptedHooks(
        on_export_begin=lambda snapshot_id: (
            export_started.set(),
            revoke_done.wait(timeout=10),
        )
    )
    r_export = Repository(repo_root, clock=clock, hooks=hooks)
    out = tmp_path / "revoked.pack"

    result_box: dict = {}

    def do_export() -> None:
        try:
            r_export.export_snapshot(sid, out)
        except BaseException as exc:  # noqa: BLE001
            result_box["error"] = exc

    t = threading.Thread(target=do_export)
    t.start()

    assert export_started.wait(timeout=10)
    # 导出尚未进入成员确定/保活事务：撤销先提交。
    Repository(repo_root, clock=clock).delete_snapshot(sid)
    revoke_done.set()
    t.join(timeout=10)
    assert not t.is_alive()

    assert isinstance(result_box.get("error"), SnapshotNotFoundError)
    assert not out.exists()
    assert r_export.list_export_pins() == []

    # 快照没了，紧接着一轮回收必须清掉这些对象（导出没有留下任何保活）。
    result = r_main.gc()
    assert result.reclaimed == sorted(ids)
    assert all(not r_main.exists(i) for i in ids)
    assert r_main.list_export_pins() == []
    assert_consistent(r_main)


def test_export_pin_wins_against_mark_revoke_and_sweep(repo_root, clock, tmp_path):
    """核心交错：保活资格先提交；随后标记、撤销、复核全部在读取窗口内完成。

    导出线程在 ``export_pin_acquired``（保活已提交、读字节之前）与主线程
    会合：主线程趁此先 gc_mark（快照仍在 → 无候选），再撤销快照，再
    gc_sweep；随后放行导出。任何对象都不允许在读取结束前消失。
    """

    r_main, ids, sid = _seed(repo_root, clock)

    pin_held = threading.Event()
    release_export = threading.Event()

    def on_pin(snapshot_id, object_ids):
        pin_held.set()
        assert release_export.wait(timeout=10)

    hooks = ScriptedHooks(on_export_pin_acquired=on_pin)
    r_export = Repository(repo_root, clock=clock, hooks=hooks)
    out = tmp_path / "winner.pack"
    result_box: dict = {}

    def do_export() -> None:
        try:
            result_box["pkg"] = r_export.export_snapshot(sid, out)
        except BaseException as exc:  # noqa: BLE001
            result_box["error"] = exc

    t = threading.Thread(target=do_export)
    t.start()
    assert pin_held.wait(timeout=10)

    # 保活引用保护期间：标记（快照仍在，候选为空）→ 撤销 → 复核。
    mark = r_main.gc_mark()
    assert mark.candidate_ids == []
    r_main.delete_snapshot(sid)
    sweep = r_main.gc_sweep(mark.run_id)
    assert sweep.reclaimed == []
    # 撤销已完成，但保活引用仍在：对象一个都不能少。
    assert all(r_main.exists(i) for i in ids)

    release_export.set()
    t.join(timeout=10)
    assert not t.is_alive()

    assert "error" not in result_box
    pkg = result_box["pkg"]
    assert pkg.object_ids == sorted(ids)
    report = verify_package(out)
    assert report.ok
    assert [m.object_id for m in report.members] == sorted(ids)
    assert all(m.digest_ok for m in report.members)

    # 导出已释放保活：新一轮标记+复核回收全部成员，不残留永久引用。
    assert r_main.list_export_pins() == []
    final = r_main.gc()
    assert final.reclaimed == sorted(ids)
    assert all(not r_main.exists(i) for i in ids)
    assert_consistent(r_main)


def test_pin_held_across_mark_revoke_sweep_then_released(repo_root, clock, tmp_path):
    """固定「标记 → 撤销 → 复核」整段都落在保活窗口内的交错。

    时间线（用 ``export_pin_acquired`` 屏障把导出停在保活已提交、读字节前）：

    1. 保活引用先提交（快照此时仍在）；
    2. 窗口内 gc_mark：成员被快照/保活双重保住，只有无关垃圾成为候选；
    3. 窗口内撤销快照——保活引用成为成员唯一的根；
    4. gc_sweep：垃圾被回收，成员既非候选又有保活引用，文件一个都不能删；
    5. 放行导出读完并释放保活；下一轮回收才允许删除成员。

    注：快照成员不可能以“候选再被救回”的形态出现——它在任何标记时刻
    都仍被快照或保活引用判活；保活的保证是“读取窗口内绝不成为删除目标”。
    """

    r_main, ids, sid = _seed(repo_root, clock)
    garbage = r_main.put(b"other-garbage")

    pin_held = threading.Event()
    release_export = threading.Event()

    def on_pin(snapshot_id, object_ids):
        pin_held.set()
        assert release_export.wait(timeout=10)

    hooks = ScriptedHooks(on_export_pin_acquired=on_pin)
    r_export = Repository(repo_root, clock=clock, hooks=hooks)
    out = tmp_path / "ordered.pack"
    result_box: dict = {}

    def do_export() -> None:
        try:
            result_box["pkg"] = r_export.export_snapshot(sid, out)
        except BaseException as exc:  # noqa: BLE001
            result_box["error"] = exc

    t = threading.Thread(target=do_export)
    t.start()
    assert pin_held.wait(timeout=10)

    # 2-4. 标记、撤销、复核全部落在保活窗口内。
    mark = r_main.gc_mark()
    assert mark.candidate_ids == [garbage]
    r_main.delete_snapshot(sid)
    sweep = r_main.gc_sweep(mark.run_id)
    assert sweep.reclaimed == [garbage]
    assert sweep.rescued == []
    assert all(r_main.exists(i) for i in ids)

    # 5. 放行导出：包完整，随后释放保活。
    release_export.set()
    t.join(timeout=10)
    assert not t.is_alive()
    assert "error" not in result_box, result_box.get("error")
    assert verify_package(out).ok
    assert r_main.list_export_pins() == []

    # 快照已撤销、保活已释放：下一轮回收确定性删除成员。
    final = r_main.gc()
    assert final.reclaimed == sorted(ids)
    assert all(not r_main.exists(i) for i in ids)
    assert_consistent(r_main)


def test_export_against_snapshot_with_concurrent_gc_of_other_objects(
    repo_root, clock, tmp_path
):
    """导出期间 GC 回收无关垃圾：保活只罩住快照成员，垃圾照常删除。"""

    r_main, ids, sid = _seed(repo_root, clock)
    garbage = r_main.put(b"unrelated-garbage")

    pin_held = threading.Event()
    release_export = threading.Event()
    hooks = ScriptedHooks(
        on_export_pin_acquired=lambda s, members: (
            pin_held.set(),
            release_export.wait(timeout=10),
        )
    )
    r_export = Repository(repo_root, clock=clock, hooks=hooks)
    out = tmp_path / "mixed.pack"
    result_box: dict = {}

    t = threading.Thread(
        target=lambda: result_box.update(
            pkg=r_export.export_snapshot(sid, out)
        )
    )
    t.start()
    assert pin_held.wait(timeout=10)

    result = r_main.gc()  # 标记时快照仍在：只有无关垃圾是候选
    assert result.reclaimed == [garbage]
    assert all(r_main.exists(i) for i in ids)

    release_export.set()
    t.join(timeout=10)
    assert not t.is_alive()
    pkg = result_box["pkg"]
    assert verify_package(pkg.path).ok
    assert not r_main.exists(garbage)
    assert_consistent(r_main)
