"""垃圾回收阶段钩子。

钩子只用于可观测性与测试同步（阶段屏障、崩溃注入），绝不参与决策；
回收的正确性不依赖任何钩子被调用。
"""

from __future__ import annotations

from typing import Protocol


class GCHooks(Protocol):
    """回收各阶段的回调协议。所有方法都可以不实现。"""

    def mark_begin(self, run_id: str) -> None:
        """一次新的标记运行开始。"""

    def mark_before_commit(self, run_id: str, candidate_ids: list[str]) -> None:
        """候选已写入但标记事务尚未提交时触发（崩溃注入点）。"""

    def mark_end(self, run_id: str, candidate_ids: list[str]) -> None:
        """候选已在单个事务中落库，尚未删除任何文件。"""

    def sweep_begin(self, run_id: str, candidate_ids: list[str]) -> None:
        """复核阶段开始。此处阻塞即可让并发线程在两阶段之间发布快照/续约。"""

    def before_reclaim(self, run_id: str, object_id: str) -> None:
        """某个候选复核通过、删除事务持有写锁、对象行删除前一刻调用。"""

    def reclaim_committed(self, run_id: str, object_id: str) -> None:
        """元数据删除已提交、对象文件删除前一刻调用（崩溃/复活竞争窗口）。"""

    def after_reclaim(self, run_id: str, object_id: str) -> None:
        """候选文件与元数据都已删除后调用。"""

    def export_begin(self, snapshot_id: str) -> None:
        """一次快照打包导出开始（尚未确定成员、尚未插入保活引用）。"""

    def export_pin_acquired(self, snapshot_id: str, object_ids: list[str]) -> None:
        """成员清单已在单个事务内确定、导出保活引用已提交、开始读文件前调用。

        在此阻塞即可把“导出已取得保活资格、GC 标记/撤销在别处进行”的
        交错固定下来。
        """

    def export_pin_released(self, snapshot_id: str) -> None:
        """导出保活引用已释放（成功或失败路径都会调用）。"""


class NullHooks:
    """默认空实现。"""

    def mark_begin(self, run_id: str) -> None:
        pass

    def mark_before_commit(self, run_id: str, candidate_ids: list[str]) -> None:
        pass

    def mark_end(self, run_id: str, candidate_ids: list[str]) -> None:
        pass

    def sweep_begin(self, run_id: str, candidate_ids: list[str]) -> None:
        pass

    def before_reclaim(self, run_id: str, object_id: str) -> None:
        pass

    def reclaim_committed(self, run_id: str, object_id: str) -> None:
        pass

    def after_reclaim(self, run_id: str, object_id: str) -> None:
        pass

    def export_begin(self, snapshot_id: str) -> None:
        pass

    def export_pin_acquired(self, snapshot_id: str, object_ids: list[str]) -> None:
        pass

    def export_pin_released(self, snapshot_id: str) -> None:
        pass
