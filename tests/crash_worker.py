"""一次性崩溃工作进程：在指定回收/导出阶段被 SIGKILL，用于测试“进程中断后安全重试”。

用法:
    python crash_worker.py <repo_root> <phase> <run_id> [<snapshot_id> <dest> <pin_ttl_seconds>]

phase（回收）:
    mark_before_commit    标记事务提交前被杀（整笔标记回滚）
    after_mark_commit     标记事务提交后立即被杀（候选已落库，文件未动）
    before_delete_commit  复核通过、持有写锁、删除元数据之前被杀（事务随进程回滚）
    after_delete_commit   元数据删除已提交、对象文件尚未 unlink 时被杀
                          （留下无任何引用的待清理文件）

phase（导出，需附带 snapshot_id / 目标包路径 / 保活 ttl 秒）:
    export_pin_kill       成员已确定、保活引用已提交、读文件之前被杀
                          （留下带到期时间的保活引用、没有包文件）
    export_read_kill      正在读取成员字节时被杀（保活引用 + 临时包文件残留）
"""

from __future__ import annotations

import os
import signal
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from objstore.clocks import SystemClock
from objstore.hooks import NullHooks
from objstore.repo import Repository


class KillBeforeDelete(NullHooks):
    def before_reclaim(self, run_id: str, object_id: str) -> None:
        os.kill(os.getpid(), signal.SIGKILL)


class KillBeforeMarkCommit(NullHooks):
    def mark_before_commit(self, run_id: str, candidate_ids: list[str]) -> None:
        os.kill(os.getpid(), signal.SIGKILL)


def _kill_at_unlink() -> None:
    original_unlink = Path.unlink

    def unlink(self, *args, **kwargs):
        # sweep 复核提交后唯一的 unlink 调用就是删除该候选对象文件。
        os.kill(os.getpid(), signal.SIGKILL)

    Path.unlink = unlink  # type: ignore[assignment]


class KillAtExportPin(NullHooks):
    """导出保活引用提交后、读取任何成员字节之前被杀。"""

    def export_pin_acquired(self, snapshot_id: str, object_ids: list[str]) -> None:
        os.kill(os.getpid(), signal.SIGKILL)


def _kill_at_export_read() -> None:
    """读到第一个成员文件时被杀（此刻保活引用已提交、半包只在临时文件里）。

    打包器经 ``pathlib.Path.open`` 读取，它解析到 ``io`` 模块全局的
    ``io.open``（即 ``builtins.open`` 的同一 C 函数，但后者补丁拦不住
    路径对象调用）；按“objects/xx/<62hex>”路径特征拦截成员文件即可，
    临时包文件（.packtmp-*）的写入不受影响。
    """

    import io

    original_open = io.open

    def opening(file, mode="r", *args, **kwargs):
        if "b" in mode and isinstance(file, (str, os.PathLike)):
            p = Path(file)
            if len(p.parent.name) == 2 and p.parent.parent.name == "objects":
                os.kill(os.getpid(), signal.SIGKILL)
        return original_open(file, mode, *args, **kwargs)

    io.open = opening
    import builtins

    builtins.open = opening  # type: ignore[assignment]


def _run_export(
    root: str,
    phase: str,
    snapshot_id: str,
    dest: str,
    pin_ttl_seconds: str,
) -> int:
    if phase == "export_pin_kill":
        hooks: NullHooks = KillAtExportPin()
    elif phase == "export_read_kill":
        hooks = NullHooks()
        _kill_at_export_read()
    else:  # pragma: no cover - 仅由测试显式选择
        raise SystemExit(f"未知导出阶段: {phase}")

    repo = Repository(root, clock=SystemClock(), hooks=hooks)
    repo.export_snapshot(
        snapshot_id,
        dest,
        pin_ttl=timedelta(seconds=float(pin_ttl_seconds)),
    )
    return 0


def main() -> int:
    root, phase, run_id = sys.argv[1], sys.argv[2], sys.argv[3]

    if phase in ("export_pin_kill", "export_read_kill"):
        return _run_export(root, phase, sys.argv[4], sys.argv[5], sys.argv[6])

    hooks: NullHooks = NullHooks()
    if phase == "before_delete_commit":
        hooks = KillBeforeDelete()
    elif phase == "after_delete_commit":
        _kill_at_unlink()
    elif phase == "mark_before_commit":
        hooks = KillBeforeMarkCommit()

    repo = Repository(root, clock=SystemClock(), hooks=hooks)

    mark = repo.gc_mark(run_id=run_id)
    if phase == "after_mark_commit":
        os.kill(os.getpid(), signal.SIGKILL)

    repo.gc_sweep(mark.run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
