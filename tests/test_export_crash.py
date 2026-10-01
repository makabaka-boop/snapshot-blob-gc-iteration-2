"""导出进程被 SIGKILL 后：不留半包、保活引用按 TTL 失效、一致性检查与回收可继续。"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

from objstore import Repository, SnapshotNotFoundError, verify_package
from objstore.clocks import SystemClock

from conftest import assert_consistent

WORKER = Path(__file__).with_name("crash_worker.py")


def _run_export_crash_worker(root, dest, phase, sid, pin_ttl_seconds):
    proc = subprocess.run(
        [
            sys.executable,
            str(WORKER),
            str(root),
            phase,
            "ignored-run-id",
            sid,
            str(dest),
            str(pin_ttl_seconds),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == -9, (
        f"工作进程应被 SIGKILL，实际 {proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )


def _seed(root):
    with Repository(root, clock=SystemClock()) as repo:
        members = [repo.put(f"export-crash-{i}".encode() * 100) for i in range(3)]
        sid = repo.publish_snapshot(members)
        assert_consistent(repo)
    return members, sid


def test_export_killed_after_pin_acquired(tmp_path):
    """保活引用已提交、读字节前被杀：引用按 TTL 到期，绝不永久保活。"""

    root = tmp_path / "repo"
    dest = tmp_path / "delivered.pack"
    members, sid = _seed(root)

    _run_export_crash_worker(root, dest, "export_pin_kill", sid, 2)

    # 没有交付包（连临时文件都不应出现在目标目录）。
    assert not dest.exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".packtmp")]

    with Repository(root, clock=SystemClock()) as repo:
        assert_consistent(repo)
        assert all(repo.exists(o) for o in members)
        # 中断留下了一个带到期时间的保活引用。
        assert repo.list_export_pins() != []

        # TTL 未到：快照仍在，撤销后回收也不能删成员（保活引用仍有效）。
        repo.delete_snapshot(sid)
        early = repo.gc()
        assert early.reclaimed == []
        assert all(repo.exists(o) for o in members)
        assert_consistent(repo)

    # 等待 TTL 到期后新进程打开：标记清理过期引用，回收正常完成。
    time.sleep(2.2)
    with Repository(root, clock=SystemClock()) as repo:
        assert_consistent(repo)
        late = repo.gc()  # 标记阶段先清理已到期的中断引用
        assert repo.list_export_pins() == []
        assert late.reclaimed == sorted(members)
        assert all(not repo.exists(o) for o in members)
        assert_consistent(repo)

        # 快照已撤销：重导也确定性失败。
        try:
            repo.export_snapshot(sid, tmp_path / "again.pack")
        except SnapshotNotFoundError:
            pass
        else:  # pragma: no cover
            raise AssertionError("撤销后的快照不应可导出")


def test_export_killed_during_read_leaves_no_half_package(tmp_path):
    """读取成员字节时被杀：目标包不存在；中断引用到期后重试得到完整包。"""

    root = tmp_path / "repo"
    dest = tmp_path / "delivered.pack"
    members, sid = _seed(root)

    _run_export_crash_worker(root, dest, "export_read_kill", sid, 1)
    assert not dest.exists()

    # 新进程立即打开：一致性检查照常通过，成员一个不少。
    with Repository(root, clock=SystemClock()) as repo:
        assert_consistent(repo)
        assert all(repo.exists(o) for o in members)
        assert repo.list_export_pins() != []  # 中断引用尚在 TTL 内

    # TTL 过后，原有回收流程顺带清掉中断引用（快照仍在，成员本就存活）。
    time.sleep(1.2)
    with Repository(root, clock=SystemClock()) as repo:
        assert_consistent(repo)
        result = repo.gc()
        assert result.reclaimed == []
        assert repo.list_export_pins() == []
        assert all(repo.exists(o) for o in members)

        # 重试导出：完整包可独立校验通过，成功后不留保活引用。
        retry_pkg = repo.export_snapshot(
            sid, dest, pin_ttl=timedelta(minutes=5)
        )
        report = verify_package(retry_pkg.path)
        assert report.ok
        assert [m.object_id for m in report.members] == sorted(members)
        assert all(m.digest_ok for m in report.members)
        assert repo.list_export_pins() == []
        assert_consistent(repo)


def test_consistency_check_and_gc_run_while_pin_is_active(tmp_path):
    """保活引用存活期间，原有的一致性检查与回收照常工作（只回收无关垃圾）。"""

    root = tmp_path / "repo"
    dest = tmp_path / "held.pack"
    members, sid = _seed(root)

    with Repository(root, clock=SystemClock()) as setup:
        garbage = setup.put(b"totally-unrelated")

    _run_export_crash_worker(root, dest, "export_pin_kill", sid, 5)

    with Repository(root, clock=SystemClock()) as repo:
        assert_consistent(repo)
        result = repo.gc()  # 保活引用未到期：成员保住，垃圾照删
        assert result.reclaimed == [garbage]
        assert all(repo.exists(o) for o in members)
        assert_consistent(repo)
