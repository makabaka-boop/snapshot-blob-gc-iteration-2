"""本地内容寻址对象仓库：不可变快照、SQLite 元数据、两阶段垃圾回收与快照打包导出。"""

from .exceptions import (
    DuplicateGCRunError,
    InternalConsistencyError,
    InvalidObjectIdError,
    LeaseNotFoundError,
    ObjectNotFoundError,
    PackageCapacityExceededError,
    PackageVerificationError,
    RepositoryError,
    SnapshotNotFoundError,
    UnknownGCRunError,
)
from .clocks import Clock, SystemClock
from .hooks import GCHooks
from .package import (
    DEFAULT_PACKAGE_MAX_BYTES,
    PackageMemberReport,
    PackageReport,
    SnapshotPackage,
    verify_package,
)
from .repo import GCMarkResult, GCSweepResult, Repository, SnapshotInfo, LeaseInfo
from .diagnostics import ConsistencyReport, check_consistency

__all__ = [
    "Clock",
    "DEFAULT_PACKAGE_MAX_BYTES",
    "SystemClock",
    "ConsistencyReport",
    "DuplicateGCRunError",
    "GCHooks",
    "GCMarkResult",
    "GCSweepResult",
    "InternalConsistencyError",
    "InvalidObjectIdError",
    "LeaseInfo",
    "LeaseNotFoundError",
    "ObjectNotFoundError",
    "PackageCapacityExceededError",
    "PackageMemberReport",
    "PackageReport",
    "PackageVerificationError",
    "Repository",
    "RepositoryError",
    "SnapshotInfo",
    "SnapshotNotFoundError",
    "SnapshotPackage",
    "UnknownGCRunError",
    "check_consistency",
    "verify_package",
]
