# Skills 与会话文件运行手册

## 部署前提

Skills、会话文件与知识库均已收敛为后端默认无条件装配的核心能力，统一共用单桶对象存储
配置（`ATHENA_STORAGE_*`），在同一个私有桶内按业务前缀严格隔离：文件与技能域仅操作
`session-files/`、`session-files-extracted/`、`session-artifacts/`、`skill-logs/`、
`skills/`，知识库原件位于 `knowledge/`。`minio`、`packaging`、`PyYAML`、
`python-multipart` 以及轻量化文档提取/图像处理依赖（`pypdfium2`、`python-docx`、
`openpyxl`、`python-pptx`、`Pillow`、`jieba`）均已属于核心依赖；可选分组 `knowledge`
仅用于重量级本地 OCR（PaddlePaddle + PaddleOCR）。

API 与 Run Worker 必须：

- 使用同一 `ATHENA_DATABASE_URL`、统一对象存储 `ATHENA_STORAGE_*` 与远程
  `ATHENA_EMBEDDING_BASE_URL` 配置；
- 使用兼容的 Python 3.12+ 运行环境与同一套平台预装依赖；
- 能访问同一个私有、已预建的 MinIO/S3 桶；
- 对 `ATHENA_SKILL_TEMP_ROOT` 具有创建、写入和删除目录的权限。

不同环境（开发、测试、生产）必须使用各自独立的私有桶，严禁混用或开启匿名读取；
同环境内知识库与文件/技能通过统一单桶前缀隔离。MinIO 服务账号至少需要桶检查、对象
列举、读取、上传和删除权限；应用不会创建桶或修改桶策略。

```text
ATHENA_STORAGE_ENDPOINT=host[:port]
ATHENA_STORAGE_ACCESS_KEY=<secret manager 注入>
ATHENA_STORAGE_SECRET_KEY=<secret manager 注入>
ATHENA_STORAGE_BUCKET=<预建私有桶>
ATHENA_STORAGE_SECURE=true
ATHENA_SKILL_TEMP_ROOT=/srv/athena/skill-executions
ATHENA_SKILL_PYTHON_EXECUTABLE=/srv/athena/.venv/bin/python
```

API 与 Worker 都读取 `ATHENA_SKILL_PYTHON_EXECUTABLE`；未设置时分别使用各自的
`sys.executable`。它们既用该解释器真实探测依赖，也用它执行脚本，因此多主机、
多容器或虚拟环境部署应填写同一可信脚本环境的绝对 Python 路径。探测会核对目标
解释器类型、版本、stdlib marker 和 `importlib.metadata` 发行包，不会用 API 或
Worker 当前环境推断目标环境。脚本不是沙箱边界；平台按产品规则仍允许可信脚本使用
其宿主进程权限和网络。本功能没有恢复脚本 CPU、内存、临时磁盘、网络或执行次数配额。

## 配额与保留边界

- 用户上传单个文件最多 50 MiB；该限制不适用于脚本生成产物。
- 每个会话的上传文件和已发布脚本产物合计最多 100 个，发布事务统一校验。
- Skill ZIP 最多 25 MiB，解压后最多 100 MiB、500 个文件，包内单文件最多 25 MiB。
- `SKILL.md` 指南最多 100,000 字符。
- 单次执行 stdout/stderr 持久化日志合计最多 5 MiB，超出部分继续排空但不保留。
- 脚本临时目录在正常、失败、取消和执行器异常路径尽力清理。清理失败时记录稳定
  `cleanup_error_code`；Worker 重启或周期任务不扫描、自动推断身份或删除遗留目录。

不得给 `skills/`、`session-files/` 或 `session-artifacts/` 配置仅按对象前缀年龄
删除的生命周期规则。数据库仍可能引用有效对象；前缀 TTL 会造成历史文件和不可变
Skill 版本不可下载。API、Run Worker 和管理写路径都不执行在线对象回收，也不维护
发布预留、owner 存活判断或持久扫描游标。对象写入仍遵循“先写对象、后提交数据库
引用”；事务失败或提交结果不确定的已知对象保守保留，等待下一次停写维护。

离线维护入口只处理合法的 `session-files/`、`session-files-extracted/`、
`session-artifacts/`、`skill-logs/` 和 `skills/` 对象，不会触碰同桶内的 `knowledge/`
前缀。默认命令是 dry-run：

```sh
PYTHONPATH=backend/src .venv/bin/python -m cli.file_maintenance
```

真实删除必须同时显式传入两个参数：

```sh
PYTHONPATH=backend/src .venv/bin/python -m cli.file_maintenance \
  --execute --confirm-all-writers-stopped
```

执行前必须停止并确认 API、Run Worker、管理迁移脚本、离线导入任务及其他可能写入同一
桶的进程均已结束；把“确认参数”只当作操作者承诺，不把它当作系统已自动验证的分布式
锁。命令默认宽限期 86400 秒，分页最多 1000 个对象、删除批最多 100 个；每次命令从
桶起点开始，`after_key` 只在本次进程内分页，不使用 PostgreSQL cursor、generation
或 CAS。每个候选都重新查询 `session_files.object_key`、`skill_versions.object_key`
和非空的 `skill_executions.log_object_key` 三处引用；任一命中、引用查询失败或单项
删除失败都保留对象并报告失败计数。命令不回显凭据、对象 URL 或文件正文。

集成测试如果使用共享桶，必须使用类似 `_RecordingStore` 的包装层。底层列举 API
可能返回其他对象元数据，但包装层只能向维护器暴露本测试 `created_keys` 记录的对象，
不得读取其他对象正文或删除其他对象；不能因隔离 schema 为空就处理共享桶中的其他
对象。生产维护也不能与共享桶中的其他环境写入者并行。

## 数据库迁移

Skills/文件迁移链顺序为：

1. `20261117_0018`：创建 Skill/不可变版本、会话文件、脚本执行审计、Run 固定技能
   快照与输入文件集合。
2. `20261118_0019`：为工具调用增加并冻结普通用户安全审批投影。
3. `20261119_0020`：曾增加在线发布预留；该结构由 `0023` 删除。
4. `20261120_0021`：曾增加持久回收游标；该结构由 `0023` 删除。
5. `20261121_0022`：曾为游标增加 generation；该列随表由 `0023` 删除。
6. `20261122_0023`：删除 `file_object_publications` 和
   `file_object_reclamation_cursors`。downgrade 只恢复空结构与游标初值，不恢复历史
   预留或扫描位置。

升级前：

1. 停止 Run Worker，等待正在执行的脚本和数据库事务收敛；
2. 停止 API 写流量或进入维护窗口；
3. 备份 PostgreSQL，并确认文件 MinIO 桶的备份或版本保留策略覆盖本次写入；
4. 在隔离环境对生产备份演练迁移。

执行：

```sh
.venv/bin/alembic -c backend/alembic.ini heads
.venv/bin/alembic -c backend/alembic.ini upgrade head
```

确认唯一 head 为 `0001_initial_schema` 后，启动全部 API 实例和新版 Worker。不要用旧版
Worker 处理已升级数据库中的新工具调用。旧 Run 迁移后技能快照为空，系统不会把它
事后绑定到当前 Skill 目录。

已在隔离 schema 中验证完整升级到唯一 head `0001_initial_schema`（历史增量迁移已合并为基线脚本）。
生产回滚不要直接执行全链 downgrade：
`0001_initial_schema` 的 downgrade 会删除全量业务表，MinIO 对象不会随之删除。旧版
在线 GC 代码已从当前应用删除，不能仅靠重升/降级恢复旧写路径。需要回滚时恢复已演练的
数据库备份，并保持对应 MinIO 对象不删除。

## 安全与故障恢复

每次脚本启动后，Runner 在调用独占的 `0700` 工作目录内以 `0600` 写入
`.athena-process-diagnostic.json`。记录只包含 Run ID、工具调用 ID、Worker 实例 ID、
脚本 PID、启动时 PGID、记录时间和工作目录，供管理员人工排查。它不是进程身份、
存活证明或跨重启锁；不得仅凭其 PID/PGID 自动发信号，目录缺失该记录也不能解释为
脚本已经结束。

当前进程内仍通过 `asyncio.subprocess.Process`、`start_new_session=True`、
`killpg`、直接子进程等待及 Linux `/proc` 僵尸判断收敛进程组。Worker 启动和周期任务
不再扫描临时根目录，不再推断 boot ID/PID namespace/starttime，也不重试删除崩溃遗留
目录。Worker 被 `SIGKILL` 或宿主崩溃时，管理员必须结合宿主进程、cwd/fd 和业务状态
人工确认，在停写窗口处置；系统不承诺跨重启自动强清理。

普通用户的 Run 详情、工具详情、消息、事件和 SSE 只暴露冻结名称/版本、白名单参数
摘要、安全输入文件元数据与已发布产物引用。原始参数、`outcome.data`、日志对象键、
宿主路径、环境变量和凭据不会通过普通用户接口返回。技能调用即使投影损坏也按
技能身份清空原始参数和结果数据；缺少必需投影时服务端拒绝批准但仍允许拒绝。
管理员执行摘要和日志使用独立管理接口并即时复核管理员角色。

会话文件文本读取和全部对象 SHA-256 校验都按固定大小块在后台线程执行。取消请求会在
后续块边界停止 UTF-8 解码、JSON 增量验证或哈希读取，并由异步调用方等待线程退出后
再返回取消；不会把整个 TXT/CSV/JSON 或文件一次性载入事件循环内存。xlsx、docx 和
pptx 使用标准库 `zipfile.ZipFile`，读取中央目录后先检查每个
`ZipInfo.orig_filename`，拒绝 NUL、控制字符、反斜杠、绝对路径、空段、`.` 和 `..`；
路径扫描完成后再核对 `[Content_Types].xml` 与各格式 root marker。校验不解压成员、
不读取正文、不调用 `testzip()`。ZIP 中央目录和 `infolist()` 会随成员元数据线性占用
内存；构造 `ZipFile` 期间没有取消注入点，后台线程收到取消后需等待构造完成，再在
marker/成员边界停止且不上传对象。脚本生成产物没有单文件 50 MiB 限制，50 MiB 只约束
用户上传。

运行故障按以下顺序处理：

1. Run 工具结果 `unknown` 时不自动重跑；重试需要用户显式确认可能重复副作用。
2. 文件对象已上传但事务回滚或提交状态不明时先保留。确认可以进入停写窗口后，先跑
   离线维护 dry-run；只有全部写入者停止时才对宽限期外、三张引用表均无命中的对象
   显式执行删除。不得按对象年龄或隔离 schema 的空引用盲删共享桶对象。
3. 临时目录或进程组清理失败时记录 `cleanup_error_code`。当前执行路径继续尽力收敛；
   Worker 重启不自动扫描或重试，遗留目录由管理员人工确认后处置。
4. MinIO 不可用时停止新的上传和脚本产物发布，保留数据库状态，修复存储后按幂等
   请求重试；不要通过重跑已执行脚本补救未提交结果。
5. 修改 API 或 Worker 的 `ATHENA_SKILL_PYTHON_EXECUTABLE`、依赖和 MinIO 配置后
   必须同步重启两类进程，并重新通过管理员接口检查 Skill 依赖。

进程组只能覆盖仍留在启动会话中的进程。可信脚本若主动调用 `setsid` 或改变进程组，
其后代不再由 Runner 的进程组收敛保证。Worker 被 `SIGKILL` 或宿主异常崩溃时，诊断
JSON 只能作为排查线索；无法确认状态时必须保留并人工处理，不能据此自动杀复用 PID。

## 验收命令

以下真实集成测试要求隔离的 PostgreSQL 与文件 MinIO 配置：

```sh
source scripts/load-test-env.sh
.venv/bin/python -m pytest -q \
  backend/tests/integration/test_skills_files_e2e_postgres_minio.py
.venv/bin/python -m pytest -q -m integration \
  backend/tests/integration/test_run_tools_postgres.py
```

`test_skills_files_e2e_postgres_minio.py` 覆盖管理员导入/启用、真实上传、Run 创建、
动态审批、真实 Python 子进程、两个产物原子发布、分别下载、普通用户安全投影、
下一 Run 文件引用、三类数据库引用保护和离线维护回收。共享测试桶通过
`_RecordingStore.created_keys` 过滤维护候选；底层能够列举桶内其他对象的元数据，
但不会读取其他正文，也不会删除任何非本测试对象。
`test_run_tools_postgres.py` 同时覆盖原子事务、UNKNOWN、幂等重查、并发配额，以及
只在隔离 schema 中执行 `0023 -> 0022 -> 0023` 的迁移往返。测试使用真实 PostgreSQL
和 MinIO，LLM 使用测试内可控 provider；真实供应商兼容性和生产部署需另行联调。
