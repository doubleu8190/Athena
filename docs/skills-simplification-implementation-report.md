# Skills 精简实施报告

记录日期：2026-11-12。当前状态：后端 Reviewer 第 1 复审 PASS；前端、LLM 和
harness Reviewer 均 PASS；Root 已接受本轮精简代码与受控 E2E 范围。本报告记录整体
实施、复审修复与统一验收证据；
[Skills 实施与验收状态](skills-implementation-status.md)中的历史结果保持原样。

复审口径更正：此前“共享桶包装器只枚举 `created_keys` / 未扫描其他对象”的表述不准确。
实际底层列举可以返回其他对象的元数据，但包装器只把本测试 `created_keys` 暴露为维护
候选并只允许删除这些对象；其他对象正文不读取，也绝不删除。旧测试结果和时间线继续
保留，本说明只修正边界事实。

## 整体实施摘要

### 进程与 Skill 执行

删除了跨重启进程身份、Worker 启动及周期遗留目录扫描和相关锁。当前执行链路保留
`asyncio.subprocess.Process`、独立工作目录、`start_new_session=True`、PGID
`killpg` 收敛、Linux `/proc` 僵尸判断、stdout/stderr 合计 5 MiB 有界日志以及
`finally` 清理。进程诊断只记录 Run、调用、Worker、PID、PGID、时间和目录，供人工
排查，不是跨重启存活证明，也不能据此自动杀进程。执行效果无法确认时继续收敛为
UNKNOWN，不自动重跑。

### 在线 GC 删除与离线维护

删除了在线对象 GC、发布预留、owner、持久 cursor、generation、维护 advisory lock
及相关调用。`20261122_0023` 从迁移链中删除 `file_object_publications` 和
`file_object_reclamation_cursors`，未改写已有迁移。

回收改为停止全部写入者后人工执行的离线 CLI：默认 dry-run；显式
`--execute --confirm-all-writers-stopped` 才删除；每次从桶起点分页且不保存游标。
每个候选逐个检查 `session_files.object_key`、`skill_versions.object_key` 和
`skill_executions.log_object_key`，任一引用命中、引用查询失败或单项删除失败都保留
对象并计入失败。提交不确定先重查，已提交结果幂等返回；多产物在同一业务事务中原子
发布，整组失败时不可见。

### 文件与 Office

会话文件上传、下载、文本读取、配额和脚本产物发布保留 owner/session 授权及单一
事务边界。Office xlsx/docx/pptx 校验改用标准库 `zipfile`：先检查每个
`ZipInfo.orig_filename` 的控制字符、反斜杠、绝对路径、空段、`.` 和 `..`，再核对
`[Content_Types].xml` 与格式 root marker；不解压、不读取成员正文、不调用
`testzip`。接受中央目录元数据随成员数线性占用内存，以及 `ZipFile` 构造期间无法
响应取消的边界。

### 前端终态与 LLM 诊断

前端保留技能管理、会话文件、动态审批和安全用户投影，并实现 Run 终态 refetch、
同 Run 最终 assistant 消息对账、刷新后持久消息恢复、旧交付缓存失效与 503 重试。
scripted 全栈覆盖管理员发布、普通用户审批下载、刷新恢复、后续 Run 引用、拒绝审批
和取消，不以暂时 SSE 状态替代数据库终态。

真实模型的失败诊断只保留脱敏结构。real live02 中两个 tool call 具有不同 index 和
不同 ID，没有重复 index/ID；根因仍不能据此确定。现有
`parallel_tool_calls=false` 和并行策略未修改，也未把该错误当作可重试错误。由于无法
取得连续 3 次全绿，Root 已停止继续尝试。

## Review 结果处理

后端 Reviewer 在第 1 复审中确认维护/CLI 定向 `13 passed`、fake 分页回归
`1 passed`，所有 finding 已关闭并返回 PASS。前端、LLM 和 harness Reviewer 也均
返回 PASS。Root 据此接受本轮精简代码和受控 E2E 范围。

这些结论不等于真实模型连续 3 次通过，也不等于已生产部署或外部模型全绿。real live02
仍未满足连续 3 次门槛，`0023` 仍未应用到业务 dev，部署和维护窗口仍受文末边界约束。

### Finding 1：全量 integration 完成时序

该 finding 由 Root 独立证据关闭。Root 查到全量后端 integration 已完成、进程退出码为 0、
实际耗时 70.297 秒且 `44 passed`，并向 Reviewer 澄清先前“运行中”消息是时序误读。
Implementer 未为同一结果无理由重复全量套件，历史及当前结论继续保留 `44 passed`。

### Finding 2：共享桶过滤后逻辑分页提前结束

`backend/tests/integration/test_skills_files_e2e_postgres_minio.py` 中的
`_RecordingStore.iter_maintenance_candidates()` 已改为跨底层物理页填充逻辑页：

- 底层页过滤后即使没有或不足 `maximum_items` 个本测试对象，也继续使用最后读取的
  底层 key 向后分页，直到填满逻辑页或确认底层耗尽。
- 新增纯 fake 回归，使用 1005 个非本测试对象夹住两个 `created_keys`，逻辑页大小为
  10。测试验证两个本测试对象都被返回，且底层被读取超过 10 页。
- 回归不创建额外的真实共享桶对象；`delete()` 继续拒绝删除任何非本测试 key，
  真实 E2E 的底层列举可能返回共享桶其他对象的元数据，但交给维护器的候选和允许删除
  的对象都只来自本测试 `created_keys`。

### Finding 3：CLI `main()` 安全接线

新增 `backend/tests/test_file_maintenance_cli.py`，全部依赖使用 mock，不连接网络：

- 无切换参数时调用一次 `_maintain(execute=False, writers_stopped=False)`，输出
  `mode=dry-run`。
- 仅传 `--execute` 时在依赖创建前以状态 2 拒绝执行，要求
  `--confirm-all-writers-stopped`。
- `--execute --confirm-all-writers-stopped` 和 retention/page/delete 参数原样透传，
  双确认均为真。
- `failed_count` 非零时返回状态 1，并输出稳定汇总。
- 依赖抛异常时只输出 `file_maintenance_failed` 并以状态 1 退出，不回显异常细节。

生产执行代码、回收算法、迁移及 Worker 生命周期未因这两项测试 finding 改动。

## 本轮命令与结果

以下命令均在本工作区实际完成：

```sh
.venv/bin/python -m pytest -q \
  backend/tests/test_file_maintenance_cli.py \
  backend/tests/test_file_reclamation.py
```

结果：`13 passed in 0.34s`。

```sh
source scripts/load-test-env.sh
.venv/bin/python -m pytest -q \
  backend/tests/integration/test_skills_files_e2e_postgres_minio.py
```

结果：`2 passed, 1 warning in 5.10s`。两项分别是新增的纯 fake 跨页回归和原有真实
PostgreSQL、隔离 schema、MinIO、Python 子进程 Skills E2E。唯一 warning 是 Alembic
配置未设置 `path_separator` 的既有弃用提示，不影响结果。底层可能列举共享测试桶中
其他对象的元数据，但包装器不把其他 key 暴露给维护器，不读取其他对象正文，也不删除
任何非本测试 `created_keys`。

定向静态检查：

- `ruff check`：修改的 CLI、维护测试和 Skills/MinIO E2E 文件全部通过。
- 严格 `mypy`：对应 3 个源/测试文件通过。
- `git diff --check`：通过。
- `docker compose -f compose.yml config --quiet`：通过，无配置或密钥输出。

## Root 提供的统一验收证据

本轮收尾由 Root 汇总以下独立结果，本报告不把它们改写成 Implementer 重跑：

- scripted03：`4 passed`，约 1.9 分钟。
- 后端完整 unit：`837 passed, 1 skipped, 46 deselected`。排除 integration、
  `llm_live` 和 `real_ocr`。
- CLI 与文件维护定向套件由 Root 独立复跑：`13 passed`，0.43 秒。
- 前端：`256 tests passed`。
- 全量后端 integration：已完成、exit 0、70.297 秒、`44 passed`。
- real live01 主链通过，约 45.5 秒，原模型配置未修改。
- real live02 失败时脱敏结构显示两个独立 tool-call index、两个不同 ID，没有重复
  index/ID；因无法满足连续 3 次通过门槛，Root 已停止第三次尝试，并保持
  `parallel_tool_calls=false` 与现有并行策略不变。
- Root 最终清理审计已完成：共审计 24 个 manifest；owned schema、bucket 和 Redis
  集合均为 `[]`；测试使用的 6 个端口全部关闭；旧 `8000/5173` 服务均返回 HTTP 200；
  业务 dev 的 `public` 迁移版本仍为 `0019`。

## 证据链接

- [scripted-03 全栈证据目录](../var/skills-simplification-e2e/scripted-03/)
- [real live01 证据目录](../var/skills-simplification-e2e/real-live-01/)
- [real live02 脱敏模型诊断](../var/skills-simplification-e2e/real-live-02/model-diagnostics.json)
- [最终清理审计](../var/skills-simplification-e2e/cleanup-audit.json)

## 配置与部署边界

### 公共目录依赖探测缓存增量修复

公共目录 GET 原先会为每个 Skill 启动一次目标解释器环境探测。现由每个
`DependencyChecker` 实例持有完整环境快照，默认 TTL 为 60 秒 monotonic 时间；首次和
过期并发 miss 在 `threading.Lock` 内只执行一次探测，其他请求复用同一快照。缓存命中
沿用实际探测的 `checked_at`，因此重复 GET 不会仅因时间推进而重写依赖检查。

只有 `public_directory` 使用 `check_cached`。import、configure 和执行前 recheck 仍
默认强制 fresh 探测并更新共享快照；fresh 失败会立即清除已有快照，即使 TTL 未过期，
后续缓存请求也必须重新探测。过期探测失败同样不返回旧快照，后续请求可再次尝试。
Skill、版本、权限和目录均继续实时读取数据库，没有新增 Redis、表、后台任务、环境
配置体系或按 Skill 的无限结果缓存。

probe 计数回归覆盖 10 个 Skill 冷启动一次探测、重复/并发 GET、TTL 过期并发刷新、
fresh 成功替换与失败失效、过期失败恢复、checker 实例隔离，以及两次公共目录读取只在
首次写回检查。依赖测试 `12 passed`，相关 Skill 服务/恢复回归 `22 passed`；定向
Ruff、mypy 和 diff 检查通过。

### Probe 协议与参数 Schema 精简增量

依赖 probe 改为固定 stdlib 脚本直接输出纯 JSON，父进程使用 `subprocess.run` 的
`capture_output=True` 和 `text=True` 接收，再由小型 Pydantic 协议模型验证完整结构。
保留目标 Python 绝对路径、`-I`、10 秒超时、真实目标 marker/发行包版本和宿主环境
禁回退。删除临时 cwd、双临时输出文件、8 MiB 上限、50000 个发行包上限、魔法前缀和
逐字段动态校验；接受可信管理员环境下先捕获输出的内存边界。`implementation_version`
改为与 packaging default_environment 等价的 final/预发布拼接。

参数 Schema 删除固定 key 白名单和自建递归 walker，使用 Draft 2020-12 的
`check_schema` 一次检查结构和标准 validator 验证参数。根省略 `type` 时只在副本上补
`object`，显式非 object 根仍拒绝；支持 `allOf`、`anyOf`、`$defs`、本地 `$ref`、
`pattern` 和 boolean 子 Schema。`format` 保持库默认 annotation 语义。验证器使用显式空
`referencing.Registry()`，外部引用稳定失败且不回显 URL，不执行隐式网络 I/O；核心依赖
显式声明 `referencing>=0.28.4,<1`。

本轮相关定向测试 `28 passed`；Root 独立复跑 4 个测试文件为 `40 passed in 3.93s`。
Ruff、mypy 和 `git diff --check` 通过。

### Skill 包成员容错与错误定位增量

Skill ZIP 校验继续对全部非目录成员执行原始路径、加密、链接、数量和实体大小预算检查，
随后仅忽略三类明确元数据：大小写敏感路径分量 `__MACOSX`、任意层大小写敏感 basename
`.DS_Store`、根目录 casefold 后为 `README.md` 或 `requirements.txt`。目录内同名 README/
requirements 和其他隐藏文件仍进入 manifest；忽略项不展开、不 hash、不进入 files，也
不安装或解析 requirements。重复忽略项可直接跳过，正常资源仍执行 NFC/casefold 重复
检测。

`SkillPackageError` 新增可选安全 `member_path`，错误码和字符串语义不变。路径、重复、
链接、加密、单文件/总量预算、成员读取及大小错误可携带转义控制字符并限长的包内路径；
可打印 Unicode 保留原文。管理员 import/add-version 的可定位 ZIP 422 返回
`{"code":稳定码,"member_path":安全路径}` 对象 detail，无路径错误仍返回稳定码字符串；
其他非管理员或工具错误不增加路径。定向 ZIP/API 测试 `22 passed`。Root 独立复跑
5 个后端测试文件为 `48 passed in 4.16s`；前端 client+admin 定向 `23 passed in
2.25s`，owner 完整前端为 `261 unit tests passed`，typecheck 和 build 通过。

已从 `.env.prod.example` 仅移除以下三个不再读取的旧变量，未修改其他新部署字段：

- `ATHENA_SKILL_TEMP_STALE_SECONDS`
- `ATHENA_FILE_ORPHAN_RETENTION_SECONDS`
- `ATHENA_FILE_MAINTENANCE_INTERVAL_SECONDS`

同一窄改此前已应用到 Compose 后端环境段。没有应用 `0023` 到现有业务 dev 库，没有
停止或迁移旧 `8000/5173` 服务，没有启动新部署栈，没有修改 MinIO `secure=true`。
本轮未 commit、未 push。

当前遗留保持不变：没有在线自动对象回收；Worker 崩溃遗留目录需人工复核；UNKNOWN
不自动重跑；Office 使用标准库 ZIP 中央目录，接受线性元数据内存和构造期间不可取消
边界。后端、前端、LLM 与 harness Reviewer 已全部 PASS，Root 已接受本轮精简代码及
受控 E2E 范围；真实模型连续 3 次门槛仍未达到，因此不得表述为生产环境或外部模型
全绿，`0023` 也尚未应用到现有业务 dev 库或部署。
