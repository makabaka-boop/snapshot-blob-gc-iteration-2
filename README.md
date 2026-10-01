# 本地对象仓库（objstore）

一个基于 **Python API + SQLite 元数据 + 内容寻址文件目录** 的本地对象仓库，核心提供：

- **不可变快照**：快照引用一组对象，发布后即根（root），快照的对象不会被回收；
- **导出租约（lease）**：带到期时间的临时根，未到期租约保护其对象，到期自动失效；
- **两阶段垃圾回收器**：先「标记候选」，再「复核后删除文件」，两阶段之间被新快照或
  未到期租约重新引用的候选一律放弃删除；
- **复活语义**：同一内容被回收后重新写入是合法复活——旧回收运行不会删除新化身的
  文件，重试旧运行也不会报一致性错误；
- **崩溃安全**：删除顺序严格为「先提交元数据删除，再删文件」，且文件删除在写锁
  事务内复核后执行。进程在任意时刻被杀死后都可以安全重试，绝不会删掉数据库仍
  引用的文件，也绝不会让数据库指向已删除的文件；
- **有界快照打包导出**：`export_snapshot` 在单个事务里确定同一份快照的成员并
  取得带到期时间的保活引用（pin），保证全部字节读完前 GC 删不掉这些对象；
  包内按对象 id 排序保存成员清单、原始字节与整包摘要，`verify_package` 不打开
  仓库即可逐项核对。撤销先完成则报告快照不存在；文件缺失、摘要不符或超过容量
  上限一律失败且不留下半包或永久保活引用；
- **确定性行为**：对象不存在、租约过期、重复回收、与回收并发的发布、与撤销/回收
  并发的导出都有明确结果。

## 目录布局

```
objstore/
  __init__.py        # 公共 API
  repo.py            # Repository：对象/快照/租约/两阶段 GC/快照打包导出
  package.py         # 导出包格式、流式打包器与独立校验入口（不依赖仓库）
  clocks.py          # Clock 抽象（SystemClock / 测试用可控时钟）
  hooks.py           # GC/导出阶段钩子（屏障、崩溃注入），不参与正确性
  exceptions.py      # 确定性异常类型
  diagnostics.py     # 数据库 ↔ 文件目录一致性核对
  schema.sql         # SQLite 模式
tests/
  conftest.py                # FakeClock、带阶段屏障的 ScriptedHooks
  test_basic.py              # 基础语义与错误结果
  test_gc.py                 # 两阶段回收与并发竞争
  test_export.py             # 打包格式、独立校验、容量与失败原子性
  test_export_races.py       # 撤销/标记/复核与导出交错（阶段屏障）
  test_export_crash.py       # 导出进程被 SIGKILL 后的回收与重试
  test_crash_recovery.py     # 子进程 SIGKILL 后的安全重试
  crash_worker.py            # 一次性崩溃工作进程（GC / 导出阶段）
Dockerfile
docker-compose.yml         # 唯一服务 verify：一次性 pytest
requirements.txt
```

## API 快速上手

```python
from datetime import timedelta
from objstore import Repository, SystemClock

repo = Repository("./data", clock=SystemClock())

a = repo.put(b"hello")                 # 内容寻址：SHA-256 即对象 id
repo.get(a)                            # b"hello"
repo.exists(a)

sid = repo.publish_snapshot([a])       # 发布不可变快照（相同集合幂等）
repo.delete_snapshot(sid)              # 只撤销根引用，不删对象

lid = repo.grant_lease([a], ttl=timedelta(hours=1))   # 导出租约
repo.revoke_lease(lid)                 # 提前撤销（也可等其自动到期）

mark = repo.gc_mark()                  # 阶段一：标记候选（不删文件）
mark.candidate_ids
result = repo.gc_sweep(mark.run_id)    # 阶段二：逐个复核后删除
result.reclaimed                       # 确认无引用、已删除
result.rescued                         # 两阶段间被重新引用或复活、放弃删除

repo.gc()                              # 便捷方法：标记+复核一把跑完

pkg = repo.export_snapshot(sid, "./snapshot.pack")
# 有界（默认总字节 ≤ 1 GiB；max_bytes/max_objects 可调）：
#   repo.export_snapshot(sid, path, max_bytes=10_000_000, max_objects=1000)
from objstore import verify_package
report = verify_package("./snapshot.pack")  # 不打开/不写仓库，只读本文件
report.ok                              # 成员 id、逐项 SHA-256、整包摘要全部相符
report.members                         # 逐项核对结果（id / 声明大小 / 实读 / 摘要）
```

### 确定性错误

| 情况 | 结果 |
| --- | --- |
| 读取/引用不存在的对象 | `ObjectNotFoundError`（不写入任何数据） |
| 删除不存在的快照 | `SnapshotNotFoundError`；导出已撤销/不存在的快照同样如此 |
| 使用不存在的租约 | `LeaseNotFoundError`；重复 `revoke_lease` 返回 `False` |
| 导出超过容量上限（字节数/成员数） | `PackageCapacityExceededError`，不产生包文件、不留保活引用 |
| 导出包独立校验不通过（结构/成员摘要/整包摘要） | `PackageReport.ok is False`（`raise_if_bad()` 抛 `PackageVerificationError`） |
| 对不存在的 run 复核 | `UnknownGCRunError`；重复 run id 标记 → `DuplicateGCRunError` |
| 对同一 run 重复 `gc_sweep` | 幂等：已删除的不再触碰，结果不变 |

## 垃圾回收的两阶段与崩溃安全

**阶段一 `gc_mark`**（单事务）：清理已到期租约 → 计算存活集
（所有已发布快照 ∪ 所有未到期租约引用的对象）→ 其余对象作为候选写入
`gc_candidates`，状态 `candidate`。此阶段不删除任何文件。

**阶段二 `gc_sweep`**：对每个候选开启一个 `BEGIN IMMEDIATE` 写事务，按
**标记时刻** 的存活集语义重新复核：

1. 被任何快照引用（快照没有时间限制）、或在标记时刻仍未到期的租约引用、或
   两阶段之间/本次扫描期间新获得的根引用 → 置为 `rescued`，绝不删除；
   （标记时有效的租约即使在复核窗口内到期也不会删除对象，到期清理留给下一次
   回收——GC 绝不删除“标记时仍被根引用”的对象。）
2. 复核通过 → 在事务内删除 `objects` 行（外键 `ON DELETE RESTRICT` 兜底）、
   候选置 `reclaimed` → **提交**；
3. 提交之后，在**另一个写事务**里删除对象文件：删前重新确认「候选仍为
   `reclaimed` 且对象行未复活」，成立才 unlink。

这样，崩溃的后果只有两种，且都可由重试修复：

- 崩溃在提交之前：事务整体回滚，文件与数据库都保持原状；
- 崩溃在提交之后、删文件之前：只剩一个**数据库不再引用**的待清理文件
  （由 `reclaimed` 候选跟踪），重试 sweep 时补删。

**并发发布的确定性**：在删除窗口并发的 `publish_snapshot` 必须等待 IMMEDIATE 写锁；
拿到锁时元数据删除已经提交、对象行不可见，因此发布确定性地收到
`ObjectNotFoundError`，且数据库里不会出现指向已删除文件的快照引用。若发布发生在
复核之前（两阶段之间），候选转为 `rescued` 而被保留。

**复活**：内容寻址下，同一内容在回收后（包括「元数据已删、文件未删」的窗口内）
被重新写入是合法的。`put` 在插入新对象行的同一事务里把该对象历史遗留的
`reclaimed` 候选改判为 `rescued`；旧运行的文件删除事务与 `put`/发布共用同一把
写锁，复核到复活后放弃 unlink——因此「窗口内重写并立即发布」一定成功且文件
不会被旧回收删除，重试旧运行也得到确定结果而非一致性错误。复活后仍无根引用的
对象由下一轮回收正常清理。

## 有界快照打包导出与保活引用

`export_snapshot(snapshot_id, dest)` 把一份**已发布**快照导出为单个自描述包文件：

- **包格式**（`objstore/package.py`）：魔数 → 成员清单（按对象 id 升序的
  `<oid> <size>` 行）→ 空行 → 各成员**原始字节**（顺序同清单）→ 结束标记 →
  覆盖此前全部字节的 **SHA-256 整包摘要**。写入采用临时文件 + 原子改名。
- **独立校验**：`verify_package(path)` 只打开该文件、不接触仓库，逐项核对
  成员 id（64 位十六进制）、声明大小、`sha256(字节) == oid`，并复核整包摘要；
  篡改任意成员字节、改动清单（增/删/换序/改大小）、截断、追加都会失败。
- **有界**：默认总字节上限 1 GiB（`max_bytes`），可选成员数上限
  （`max_objects`）。上限在写任何字节之前判定；读取过程中再次累计复核。
  超限抛 `PackageCapacityExceededError`，目标路径不被创建或覆盖。

与「后台撤销快照 + 两阶段 GC」的竞争由一个**内部保活引用**（`export_pins` 表，
与租约同寿命语义）裁定，顺序严格为「先确定成员，再取得保活」：

1. 成员清单在**单个 IMMEDIATE 写事务**里读取；快照不存在则确定性抛
   `SnapshotNotFoundError`——撤销先于该事务完成时，导出必然报告快照不存在。
2. 同一事务里为成员插入带到期时间的保活引用并提交。提交后 GC 复核必须把成员
   判活（`_is_live` 同时查快照、未到期租约、未到期导出 pin；标记阶段顺带清理
   已到期 pin），因此撤销快照或扫描其他候选都不可能在全部字节读完前删除文件；
   反之若 GC 的删除事务先拿到写锁，则导出在该事务里确定性地看到快照/对象缺失。
   两种交错互斥，包绝不会混入两次快照状态，也不可能只有部分文件。
3. 读取每个成员时同步核对大小与内容摘要；文件缺失或损坏 →
   `InternalConsistencyError`，临时包文件被清理，**绝不交付半包**。
4. 保活引用在成功与所有失败路径上立即删除；进程中断时它按 `pin_ttl`（默认
   1 小时，可注入短 TTL）到期，下一次 `gc_mark` 自动清理——不会留下永久保活。
   中断后一致性检查与 GC 可立刻继续，导出也可用新的 pin 安全重试。

## 一致性核对

`objstore.check_consistency(repo)` 双向核对数据库与 `objects/` 目录：

- 数据库有行但文件缺失（指向已删除文件）；
- 文件存在但没有数据库行（且不在 reclaimed 待清理集合中的孤儿文件）；
- 快照/租约引用了不存在的对象；
- 回收审计状态矛盾、残留临时文件等。

测试在**每个关键步骤之后**都调用该检查。

## 测试设计：可控时钟与阶段屏障

- `FakeClock`：冻结/可步进时钟，复现「租约在标记与复核之间到期」而无需真实睡眠；
- `ScriptedHooks`：在 `mark_begin / mark_end / sweep_begin / before_reclaim /
  reclaim_committed / after_reclaim` 以及导出的 `export_begin /
  export_pin_acquired / export_pin_released` 注入回调；测试用 `threading.Barrier`
  让并发线程恰好在「两阶段之间」「删除窗口」「元数据已删、文件未删」或
  「保活资格已取得、撤销与回收并发」的位置会合；
- `crash_worker.py`：真实子进程在标记前、标记后、元数据删除提交前、文件删除前，
  以及导出的「保活引用提交后、读字节前」「读取成员字节期间」被 `SIGKILL`，
  随后由新进程打开仓库核对一致性并重试。

## 验收（固定三步，依次执行）

```bash
docker compose config --quiet     # 1. 校验 compose 文件
docker compose build              # 2. 构建 verify 镜像
docker compose run --rm verify    # 3. 一次性运行全部 pytest，退出码即验收结果
```

`verify` 服务是一次性的：pytest 全部通过则容器退出码为 0，`--rm` 自动清理。

本地不使用 Docker 时也可直接运行：

```bash
pip install -r requirements.txt
python -m pytest -v
```
