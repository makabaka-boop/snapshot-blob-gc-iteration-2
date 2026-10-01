"""有界快照打包导出：整包格式、独立校验、容量限制与失败原子性。"""

from __future__ import annotations

import hashlib

import pytest

from objstore import (
    InternalConsistencyError,
    PackageCapacityExceededError,
    SnapshotNotFoundError,
    verify_package,
)

from conftest import assert_consistent


def _package_regions(data: bytes):
    """把一个合法包切成 (成员 dict: oid -> bytes, 整包摘要)，测试辅助。"""

    from objstore.package import PACK_MAGIC, PACK_END_MARKER

    assert data.startswith(PACK_MAGIC)
    rest = data[len(PACK_MAGIC):]
    header_end = rest.index(b"\n\n") + 2
    manifest = rest[: header_end - 1].decode("ascii").splitlines()
    members: dict[str, bytes] = {}
    cursor = header_end
    for line in manifest:
        oid, size_text = line.split(" ")
        size = int(size_text)
        members[oid] = rest[cursor : cursor + size]
        assert len(members[oid]) == size
        cursor += size
    assert rest[cursor:].startswith(PACK_END_MARKER)
    tail = rest[cursor + len(PACK_END_MARKER) :].decode("ascii").strip()
    return members, tail


def _digest_prefix(data: bytes, digest_line: bytes) -> bytes:
    """整包摘要覆盖范围：摘要行之前的全部字节。"""

    idx = data.index(digest_line)
    return data[:idx]


def test_export_writes_sorted_manifest_raw_bytes_and_digest(repo, tmp_path):
    ids = [repo.put(f"member-{i}".encode() * 7) for i in range(4)]
    sid = repo.publish_snapshot(ids)  # 发布集合幂等

    out = tmp_path / "snap.pack"
    pkg = repo.export_snapshot(sid, out)

    assert out.exists()
    assert pkg.path == out
    assert pkg.snapshot_id == sid
    assert pkg.object_ids == sorted(ids)
    assert pkg.total_bytes == sum(len(f"member-{i}".encode() * 7) for i in range(4))

    data = out.read_bytes()
    # 包内清单按对象 id 升序排列，成员为原始字节，末尾是整包摘要。
    manifest_lines = data[len(b"OBJSTORE-PACK-1\n"):].split(b"\n\n", 1)[0].split(b"\n")
    assert manifest_lines == [
        f"{oid} {len(repo.get(oid))}".encode() for oid in sorted(ids)
    ]
    members, tail = _package_regions(data)
    assert set(members) == set(ids)
    for oid, blob in members.items():
        assert blob == repo.get(oid)
        assert hashlib.sha256(blob).hexdigest() == oid
    assert tail == pkg.digest == hashlib.sha256(
        _digest_prefix(data, tail.encode() + b"\n")
    ).hexdigest()

    report = verify_package(out)
    assert report.ok
    assert [m.object_id for m in report.members] == sorted(ids)
    assert all(m.bytes_read == m.size and m.digest_ok for m in report.members)
    assert report.expected_digest == report.actual_digest == pkg.digest

    # 成功导出后不存在任何保活引用。
    assert repo.list_export_pins() == []
    assert_consistent(repo)


def test_export_is_deterministic_for_same_snapshot(repo, tmp_path):
    ids = [repo.put(b"aaa"), repo.put(b"bbb")]
    sid = repo.publish_snapshot(ids)
    p1 = tmp_path / "a.pack"
    p2 = tmp_path / "b.pack"
    pkg1 = repo.export_snapshot(sid, p1)
    pkg2 = repo.export_snapshot(sid, p2)
    assert p1.read_bytes() == p2.read_bytes()
    assert pkg1.digest == pkg2.digest
    assert repo.list_export_pins() == []


def test_empty_snapshot_exports_valid_empty_package(repo, tmp_path):
    sid = repo.publish_snapshot([])
    out = tmp_path / "empty.pack"
    pkg = repo.export_snapshot(sid, out)
    assert pkg.object_ids == []
    assert pkg.total_bytes == 0
    report = verify_package(out)
    assert report.ok
    assert report.members == []
    assert_consistent(repo)


def test_export_unknown_snapshot_raises_and_writes_nothing(repo, tmp_path):
    out = tmp_path / "nope.pack"
    with pytest.raises(SnapshotNotFoundError):
        repo.export_snapshot("deadbeef" * 8, out)
    assert not out.exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".packtmp")]
    assert repo.list_export_pins() == []
    assert_consistent(repo)


def test_export_after_revoke_reports_snapshot_missing(repo, tmp_path):
    oid = repo.put(b"will-be-revoked")
    sid = repo.publish_snapshot([oid])
    repo.delete_snapshot(sid)
    out = tmp_path / "gone.pack"
    with pytest.raises(SnapshotNotFoundError):
        repo.export_snapshot(sid, out)
    assert not out.exists()
    assert repo.list_export_pins() == []
    assert_consistent(repo)


def test_capacity_by_bytes_rejected_without_half_package(repo, tmp_path):
    ids = [repo.put(b"x" * 100), repo.put(b"y" * 100)]
    sid = repo.publish_snapshot(ids)
    out = tmp_path / "too-big.pack"
    with pytest.raises(PackageCapacityExceededError):
        repo.export_snapshot(sid, out, max_bytes=150)
    assert not out.exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".packtmp")]
    assert repo.list_export_pins() == []
    assert_consistent(repo)

    # 放宽上限后同一快照可以完整导出。
    pkg = repo.export_snapshot(sid, tmp_path / "ok.pack", max_bytes=200)
    assert verify_package(pkg.path).ok


def test_capacity_by_object_count_rejected(repo, tmp_path):
    ids = [repo.put(f"n-{i}".encode()) for i in range(3)]
    sid = repo.publish_snapshot(ids)
    out = tmp_path / "too-many.pack"
    with pytest.raises(PackageCapacityExceededError):
        repo.export_snapshot(sid, out, max_objects=2)
    assert not out.exists()
    assert repo.list_export_pins() == []
    assert_consistent(repo)


def test_corrupt_source_file_aborts_whole_export(repo, tmp_path):
    a = repo.put(b"good-bytes")
    b = repo.put(b"also-good-bytes")
    sid = repo.publish_snapshot([a, b])
    out = tmp_path / "bad.pack"

    b_path = repo._path_for(b)
    original = b_path.read_bytes()
    b_path.write_bytes(b"tampered-content!!!")
    try:
        with pytest.raises(InternalConsistencyError):
            repo.export_snapshot(sid, out)
        assert not out.exists()
        assert not [p for p in tmp_path.iterdir() if p.name.startswith(".packtmp")]
        assert repo.list_export_pins() == []
        assert_consistent(repo)  # 一致性检查只看存在性：损坏已被导出捕获
    finally:
        b_path.write_bytes(original)  # 恢复，供 fixture 的一致性收尾通过

    # 恢复后完整导出成功。
    pkg = repo.export_snapshot(sid, tmp_path / "good.pack")
    assert verify_package(pkg.path).ok
    assert_consistent(repo)


def test_missing_source_file_aborts_whole_export(repo, tmp_path):
    a = repo.put(b"first-member")
    b = repo.put(b"second-member")
    sid = repo.publish_snapshot([a, b])
    out = tmp_path / "missing.pack"

    b_path = repo._path_for(b)
    original = b_path.read_bytes()
    b_path.unlink()
    try:
        with pytest.raises(InternalConsistencyError):
            repo.export_snapshot(sid, out)
        assert not out.exists()
        assert repo.list_export_pins() == []
    finally:
        b_path.parent.mkdir(parents=True, exist_ok=True)
        b_path.write_bytes(original)

    assert_consistent(repo)
    pkg = repo.export_snapshot(sid, tmp_path / "complete.pack")
    assert verify_package(pkg.path).ok


def test_verify_detects_tampered_member_bytes(repo, tmp_path):
    ids = [repo.put(b"alpha"), repo.put(b"beta" * 100)]
    sid = repo.publish_snapshot(ids)
    out = tmp_path / "snap.pack"
    pkg = repo.export_snapshot(sid, out)

    data = bytearray(out.read_bytes())
    members, _ = _package_regions(bytes(data))
    # 改动第二个成员（在文件较后位置）的一个字节：成员摘要与整包摘要都应失败。
    target = members[ids[1]]
    pos = data.index(target) + 3
    data[pos] ^= 0x01
    tampered = tmp_path / "tampered-member.pack"
    tampered.write_bytes(data)

    report = verify_package(tampered)
    assert not report.ok
    assert any(ids[1] in err for err in report.errors)
    assert report.actual_digest != pkg.digest
    bad = [m for m in report.members if m.object_id == ids[1]][0]
    assert not bad.digest_ok


def test_verify_detects_manifest_tampering(repo, tmp_path):
    oid = repo.put(b"manifest-victim")
    sid = repo.publish_snapshot([oid])
    out = tmp_path / "snap.pack"
    repo.export_snapshot(sid, out)

    data = bytearray(out.read_bytes())
    line = f"{oid} {len(b'manifest-victim')}\n".encode()
    pos = data.index(line)
    # 把清单声明大小改大 1：成员区布局与结束标记全部错位。
    data[pos : pos + len(line)] = f"{oid} {len(b'manifest-victim') + 1}\n".encode()
    tampered = tmp_path / "tampered-manifest.pack"
    tampered.write_bytes(data)
    report = verify_package(tampered)
    assert not report.ok
    assert report.errors


def test_verify_detects_truncation_and_append_and_garbage(repo, tmp_path):
    oid = repo.put(b"truncate-me-please")
    sid = repo.publish_snapshot([oid])
    out = tmp_path / "snap.pack"
    repo.export_snapshot(sid, out)

    truncated = tmp_path / "truncated.pack"
    truncated.write_bytes(out.read_bytes()[:-40])
    assert not verify_package(truncated).ok

    appended = tmp_path / "appended.pack"
    appended.write_bytes(out.read_bytes() + b"extra")
    report = verify_package(appended)
    assert not report.ok
    assert any("多余字节" in e or "摘要" in e for e in report.errors)

    garbage = tmp_path / "garbage.pack"
    garbage.write_bytes(b"this is not a package at all")
    report = verify_package(garbage)
    assert not report.ok and report.members == []


def test_verify_is_independent_of_repository(repo_root, tmp_path):
    """校验入口只打开包文件：把包复制到仓库之外并撤销快照、删除对象后仍可核对。

    通过把包复制到与仓库无关的目录、再在仓库侧撤销并回收全部成员来证明
    校验过程不依赖、也不写入仓库的任何状态。
    """

    import shutil

    from objstore import Repository

    r = Repository(repo_root)
    ids = [r.put(b"portable-1"), r.put(b"portable-2")]
    sid = r.publish_snapshot(ids)
    out = tmp_path / "travel.pack"
    r.export_snapshot(sid, out)

    away = tmp_path / "elsewhere" / "copied.pack"
    away.parent.mkdir()
    shutil.copyfile(out, away)

    # 仓库侧撤销快照并彻底回收成员；包的校验必须仍然逐项通过。
    r.delete_snapshot(sid)
    result = r.gc()
    assert result.reclaimed == sorted(ids)
    assert not any(r.exists(i) for i in ids)

    report = verify_package(away)
    assert report.ok
    assert sorted(m.object_id for m in report.members) == sorted(ids)
    assert_consistent(r)


def test_verify_missing_file_raises_native_oserror(repo, tmp_path):
    with pytest.raises(FileNotFoundError):
        verify_package(tmp_path / "never-existed.pack")


def test_report_raise_if_bad_and_member_detail(repo, tmp_path):
    from objstore import PackageVerificationError

    ids = [repo.put(b"abc"), repo.put(b"defgh")]
    sid = repo.publish_snapshot(ids)
    good = tmp_path / "good.pack"
    repo.export_snapshot(sid, good)
    verify_package(good).raise_if_bad()  # 不抛

    data = bytearray(good.read_bytes())
    members, _ = _package_regions(bytes(data))
    pos = data.index(members[ids[0]])
    data[pos] ^= 0xFF
    bad = tmp_path / "bad.pack"
    bad.write_bytes(data)
    report = verify_package(bad)
    with pytest.raises(PackageVerificationError):
        report.raise_if_bad()
    first = [m for m in report.members if m.object_id == ids[0]][0]
    assert first.bytes_read == first.size and not first.digest_ok
    second = [m for m in report.members if m.object_id == ids[1]][0]
    assert second.ok  # 逐项核对：未被篡改的成员依旧通过


def test_export_does_not_protect_objects_after_release(repo, tmp_path):
    """保活只覆盖读取窗口：导出结束后撤销快照，对象必须能被正常回收。"""

    oid = repo.put(b"ephemeral-export")
    sid = repo.publish_snapshot([oid])
    pkg = repo.export_snapshot(sid, tmp_path / "snap.pack")
    assert verify_package(pkg.path).ok
    assert repo.list_export_pins() == []

    repo.delete_snapshot(sid)
    result = repo.gc()
    assert result.reclaimed == [oid]
    assert not repo.exists(oid)
    assert_consistent(repo)
