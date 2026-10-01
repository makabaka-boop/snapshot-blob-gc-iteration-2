"""快照导出包格式、流式打包器与独立校验入口。

包是一个**单文件、自描述**容器，与仓库完全解耦：校验入口只读本文件，
不打开、更不写入任何仓库。布局为::

    OBJSTORE-PACK-1\\n               魔数
    <oid> <size>\\n                  成员清单，按对象 id 升序，每行一个
    ...
    \\n                               空行，结束清单
    <成员 0 的原始字节>              字节顺序严格等于清单顺序
    <成员 1 的原始字节>
    ...
    OBJSTORE-PACK-1-END\\n           结束标记
    <64 位十六进制 SHA-256>\\n       整包摘要（不覆盖本行自身）

整包摘要覆盖魔数、清单（含结束空行）、全部成员原始字节与结束标记，因此
清单被增删改、成员顺序被调换、任何字节被篡改、文件被截断或追加都会使
摘要不符。每个成员的字节还单独按内容寻址约定核对：``sha256(字节) == oid``
且读取长度必须等于清单声明的大小。

打包只通过临时文件 + 原子改名落盘：任何失败都不会在目标路径留下半包。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .exceptions import (
    InternalConsistencyError,
    PackageCapacityExceededError,
    PackageVerificationError,
)

PACK_MAGIC = b"OBJSTORE-PACK-1\n"
PACK_END_MARKER = b"OBJSTORE-PACK-1-END\n"
_OID_RE = re.compile(r"^[0-9a-f]{64}$")
_READ_CHUNK = 1024 * 1024

# 导出的默认容量上限：默认拒绝打包超过 1 GiB 成员字节的快照，
# 调用方可以显式放宽或收紧；None 表示不限。
DEFAULT_PACKAGE_MAX_BYTES: int | None = 1 << 30


@dataclass(frozen=True)
class SnapshotPackage:
    """一次成功导出的结果（包文件已经 fsync 并原子改名到位）。"""

    path: Path
    snapshot_id: str
    object_ids: list[str]
    total_bytes: int
    digest: str


@dataclass
class PackageMemberReport:
    """逐项校验结果：成员 id、声明大小、实际读到的字节数与内容摘要是否相符。"""

    object_id: str
    size: int
    bytes_read: int = 0
    digest_ok: bool = False

    @property
    def ok(self) -> bool:
        return self.bytes_read == self.size and self.digest_ok


@dataclass
class PackageReport:
    """整包独立校验报告。``errors`` 为空且每个成员都 ok 即整包可信。"""

    members: list[PackageMemberReport] = field(default_factory=list)
    expected_digest: str | None = None
    actual_digest: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and all(m.ok for m in self.members)

    def raise_if_bad(self) -> None:
        if not self.ok:
            detail = "\n  - ".join(self.errors) if self.errors else "成员摘要不符"
            raise PackageVerificationError(f"导出包校验失败:\n  - {detail}")


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_package(
    dest: str | os.PathLike[str],
    snapshot_id: str,
    entries: list[tuple[str, int, Path]],
    *,
    max_bytes: int | None = DEFAULT_PACKAGE_MAX_BYTES,
) -> SnapshotPackage:
    """把 ``entries``（(oid, size, path)，须已按 oid 排序）流式写入整包。

    读取每个成员时同步核对大小与内容摘要；任何成员缺失、被篡改或超出
    ``max_bytes`` 都会抛异常、清理临时文件并保证 ``dest`` 不被触碰，
    绝不会交付看似成功的半包。
    """

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 清单顺序即成员身份的一部分：无论调用方如何传参，包内一律按对象 id 排序。
    entries = sorted(entries, key=lambda e: e[0])
    fd, tmp_name = tempfile.mkstemp(prefix=".packtmp-", dir=dest.parent)
    tmp_path = Path(tmp_name)
    overall = hashlib.sha256()
    total_bytes = 0
    object_ids: list[str] = []
    digest = ""
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(PACK_MAGIC)
            overall.update(PACK_MAGIC)
            for oid, size, _path in entries:
                line = f"{oid} {size}\n".encode("ascii")
                out.write(line)
                overall.update(line)
                object_ids.append(oid)
            out.write(b"\n")
            overall.update(b"\n")

            for oid, size, path in entries:
                member_hash = hashlib.sha256()
                bytes_read = 0
                try:
                    with path.open("rb") as src:
                        while True:
                            chunk = src.read(_READ_CHUNK)
                            if not chunk:
                                break
                            bytes_read += len(chunk)
                            total_bytes += len(chunk)
                            if max_bytes is not None and total_bytes > max_bytes:
                                raise PackageCapacityExceededError(
                                    f"导出成员总字节超过上限 {max_bytes}"
                                    f"（在对象 {oid} 处超出）"
                                )
                            member_hash.update(chunk)
                            overall.update(chunk)
                            out.write(chunk)
                except FileNotFoundError:
                    # 保活事务已阻止 GC 删除；文件缺失只可能是外部损坏。
                    raise InternalConsistencyError(
                        f"数据库引用了缺失的对象文件: {oid}"
                    ) from None
                if bytes_read != size:
                    raise InternalConsistencyError(
                        f"对象文件大小与元数据不符: {oid}"
                        f"（声明 {size}，实际 {bytes_read}）"
                    )
                if member_hash.hexdigest() != oid:
                    raise InternalConsistencyError(f"对象文件内容与 id 不符: {oid}")

            out.write(PACK_END_MARKER)
            overall.update(PACK_END_MARKER)
            digest = overall.hexdigest()
            out.write(digest.encode("ascii"))
            out.write(b"\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp_path, dest)
        _fsync_dir(dest.parent)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise
    return SnapshotPackage(
        path=dest,
        snapshot_id=snapshot_id,
        object_ids=object_ids,
        total_bytes=total_bytes,
        digest=digest,
    )


def verify_package(path: str | os.PathLike[str]) -> PackageReport:
    """独立校验入口：只读取包文件本身，不打开也不写入仓库。

    逐项核对成员 id（64 位十六进制）、声明大小与内容 SHA-256，并复核
    整包摘要。任何结构损坏、字节缺失/多余、摘要不符都记录在报告中，
    不抛出异常；文件本身无法打开时抛出原生 :class:`OSError`，
    可由调用方按文件缺失处理。
    """

    report = PackageReport()
    with Path(path).open("rb") as f:
        overall = hashlib.sha256()

        magic = f.readline()
        if magic != PACK_MAGIC:
            report.errors.append("非法魔数：不是 objstore 导出包或版本不受支持")
            return report
        overall.update(magic)

        declared: list[tuple[str, int]] = []
        framing_broken = False
        while True:
            line = f.readline()
            if not line:
                report.errors.append("清单未以空行结束即遇到文件结尾（包被截断）")
                framing_broken = True
                break
            overall.update(line)
            if line == b"\n":
                break
            try:
                oid_text, size_text = line.decode("ascii").rstrip("\n").split(" ")
                size = int(size_text)
            except (UnicodeDecodeError, ValueError):
                report.errors.append(f"无法解析的清单行: {line!r}")
                framing_broken = True
                break
            if not _OID_RE.fullmatch(oid_text) or size < 0:
                report.errors.append(f"清单条目非法: {oid_text!r} {size!r}")
                framing_broken = True
                break
            declared.append((oid_text, size))

        for oid, size in declared:
            member = PackageMemberReport(object_id=oid, size=size)
            report.members.append(member)
            if framing_broken:
                continue
            member_hash = hashlib.sha256()
            remaining = size
            while remaining > 0:
                chunk = f.read(min(_READ_CHUNK, remaining))
                if not chunk:
                    report.errors.append(
                        f"成员 {oid} 声明 {size} 字节但文件提前结束"
                        f"（仅读到 {member.bytes_read} 字节）"
                    )
                    framing_broken = True
                    break
                member.bytes_read += len(chunk)
                remaining -= len(chunk)
                member_hash.update(chunk)
                overall.update(chunk)
            if framing_broken:
                member.digest_ok = False
                break
            member.digest_ok = member_hash.hexdigest() == oid
            if not member.digest_ok:
                report.errors.append(f"成员内容摘要与清单中的 id 不符: {oid}")

        if framing_broken:
            return report

        marker = f.readline()
        if marker != PACK_END_MARKER:
            report.errors.append("成员区结束标记缺失或位置错误（成员字节大小可能被篡改）")
            return report
        overall.update(marker)

        digest_line = f.readline()
        try:
            expected = digest_line.decode("ascii").rstrip("\n")
        except UnicodeDecodeError:
            report.errors.append("整包摘要行无法解析")
            return report
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            report.errors.append("整包摘要行格式非法")
            return report
        trailing = f.read(1)
        if trailing:
            report.errors.append("整包摘要之后存在多余字节（包被追加/拼接）")
        report.expected_digest = expected
        report.actual_digest = overall.hexdigest()
        if report.actual_digest != expected:
            report.errors.append("整包摘要不符：清单或成员字节已被篡改/截断")

    return report
