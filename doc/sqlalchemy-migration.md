# SQLAlchemy ORM 迁移文档

## 概述

本文档描述了 Athena 项目从 `aiosqlite` 原生 SQL 迁移到 SQLAlchemy ORM 的实现细节和注意事项。

## 迁移目标

1. 使用 SQLAlchemy ORM 替代原生 SQL 字符串
2. 实现软删除策略（禁止物理删除）
3. 保持 API 兼容性，现有代码无需修改
4. 提供类型安全和更好的 IDE 支持

## 架构变更

### 文件结构

```
athena/db/
├── __init__.py          # 模块导出
├── engine.py            # 新增：AsyncEngine + sessionmaker 工厂
├── models.py            # 新增：SQLAlchemy ORM 模型类
├── repository.py        # 新增：Repository 模式封装 CRUD
├── database.py          # 重构：兼容层，内部使用 Repository
└── schema.py            # 保留：原始 DDL（已废弃）
```

### 依赖变更

在 `pyproject.toml` 中添加：
```toml
dependencies = [
    # ... 其他依赖
    "sqlalchemy[asyncio]>=2.0.0",
    "aiosqlite>=0.20.0",  # 保留，作为 SQLAlchemy async driver
]
```

## ORM 模型设计

### 软删除策略

所有模型均采用软删除方式，每个模型包含 `deleted_time` 字段：

```python
class SessionModel(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # ... 其他字段
    deleted_time: Mapped[str | None] = mapped_column(String, nullable=True)
```

**软删除行为：**
- 删除操作将 `deleted_time` 设置为当前时间（ISO 格式）
- 所有查询方法默认过滤 `deleted_time IS NULL`
- 提供 `include_deleted` 参数用于查询包含已删除记录

### 模型与表的映射关系

| ORM 模型 | 数据库表 | 说明 |
|---------|---------|------|
| `SessionModel` | `sessions` | 会话表 |
| `MessageModel` | `messages` | 消息表 |
| `StepModel` | `steps` | 执行步骤表 |
| `ToolCallModel` | `tool_call` | 工具调用记录表 |
| `ApprovalLogModel` | `approval_logs` | 审批日志表 |

### 字段映射

**JSON 字段处理：**
- 原 `metadata` 字段 → `metadata_json`（存储 JSON 字符串）
- 原 `tool_calls` 字段 → `tool_calls_json`（存储 JSON 字符串）
- 原 `arguments` 字段 → `arguments_json`（存储 JSON 字符串）

Repository 层自动处理 JSON 序列化/反序列化，对外接口保持 `dict` 类型。

## 引擎与会话管理

### 引擎配置

```python
# athena/db/engine.py
_engine = create_async_engine(
    f"sqlite+aiosqlite:///{db_path}",
    echo=False,
    pool_size=5,
    max_overflow=10,
    connect_args={"check_same_thread": False},
)
```

### 会话管理

**重要说明：AsyncSession ≠ 数据库连接**

- SQLAlchemy 内部维护连接池（pool_size=5, max_overflow=10）
- `AsyncSession` 是轻量级的工作单元（Unit of Work），从连接池借出连接
- `session.close()` 后连接归还池，而非关闭
- 因此"每次操作新建 session"不会创建新连接，仅复用池中连接

```python
# 每个操作使用独立的 AsyncSession
async with get_session() as session:
    async with session.begin():
        # 执行数据库操作
        session.add(model)
    # 自动 commit，异常时自动 rollback
```

## Repository 层

### 设计原则

1. **每个方法接受/返回 dict**，保持与现有调用方的兼容性
2. **JSON 字段自动序列化/反序列化**
3. **事务管理**：使用 `async with session.begin()` 确保自动 commit/rollback
4. **软删除**：所有删除操作设置 `deleted_time`，查询默认过滤已删除记录

### Repository 类

| Repository | 对应模型 | 主要方法 |
|-----------|---------|---------|
| `SessionRepository` | `SessionModel` | `create`, `get`, `list_all`, `update`, `query_by_status`, `delete` |
| `MessageRepository` | `MessageModel` | `save`, `get_by_session` |
| `StepRepository` | `StepModel` | `save`, `update`, `get_by_session`, `get_by_run`, `get_last_step_number` |
| `ToolCallRepository` | `ToolCallModel` | `save`, `update`, `query` |
| `ApprovalLogRepository` | `ApprovalLogModel` | `save`, `get_by_session`, `query_by_tool_call` |

## 兼容层

`Database` 类作为兼容层，内部委托给 Repository 实例：

```python
class Database:
    def __init__(self, db_path: str) -> None:
        self._sessions = SessionRepository()
        self._messages = MessageRepository()
        # ...

    async def create_session(self, session_id: str, title: str = "New Session") -> dict[str, Any]:
        return await self._sessions.create(session_id, title)
```

**所有现有调用方无需修改**，包括：
- `athena/core/agent/workflow.py`
- `athena/core/harness/harness.py`
- `athena/core/recovery/session_recovery.py`
- `athena/gateway/routes/sessions.py`
- `athena/gateway/routes/approval.py`

## 错误处理

### 异常类型

SQLAlchemy 异常会被捕获并记录日志：

```python
try:
    async with session.begin():
        session.add(model)
except SQLAlchemyError as e:
    logger.error("database_operation_failed", error=str(e))
    raise
```

### 事务完整性

1. **自动事务管理**：使用 `async with session.begin()` 确保 commit/rollback
2. **软删除实现**：所有删除操作设置 `deleted_time`，不物理删除数据
3. **会话隔离**：每个操作使用独立的 `AsyncSession`，避免长事务
4. **并发安全**：多个 AsyncSession 可并发使用，连接池自动管理连接分配

## 测试

### 运行测试

```bash
# 运行原有测试（验证兼容性）
pytest tests/test_database.py -v

# 运行新 ORM 测试
pytest tests/test_orm.py -v
```

### 测试覆盖

**原有测试（7 个）：**
- `test_create_and_get_session`
- `test_list_sessions`
- `test_update_session_status`
- `test_save_and_get_messages`
- `test_save_step_and_query`
- `test_save_tool_call`
- `test_delete_session_cascade`

**新增 ORM 测试（12 个）：**
- `test_soft_delete_session` - 验证软删除功能
- `test_soft_delete_cascade` - 验证级联软删除
- `test_soft_delete_preserves_data` - 验证数据仍存在于数据库
- `test_transaction_rollback_on_error` - 验证事务回滚
- `test_json_field_serialization` - 验证 JSON 序列化
- `test_json_field_with_tool_calls` - 验证 tool_calls JSON
- `test_concurrent_operations` - 验证并发安全
- `test_get_nonexistent_session` - 边界情况
- `test_update_nonexistent_session` - 边界情况
- `test_empty_database_operations` - 空数据库操作
- `test_get_last_step_number_empty` - 空 run_id
- `test_get_last_step_number` - 最大步骤号

## 回滚方案

如需切换回旧实现：

1. 恢复 `athena/db/database.py` 到原始版本
2. 恢复 `athena/db/schema.py` 的使用
3. 移除新增文件：
   - `athena/db/models.py`
   - `athena/db/engine.py`
   - `athena/db/repository.py`
4. 从 `pyproject.toml` 移除 `sqlalchemy[asyncio]` 依赖

## 注意事项

1. **软删除不可逆**：一旦设置 `deleted_time`，数据不会被物理删除。如需恢复，需手动将 `deleted_time` 设为 `NULL`。

2. **JSON 字段大小**：SQLite 对 JSON 字段大小无限制，但建议单条记录不超过 1MB。

3. **连接池配置**：
   - `pool_size=5`：核心连接数
   - `max_overflow=10`：超出核心数的临时连接
   - 对于 SQLite 单文件数据库，通常 5 个连接已足够

4. **并发写入**：SQLite 支持并发读取，但写入是串行的。SQLAlchemy 会自动处理写锁。

5. **数据迁移**：本次迁移保持表结构兼容，现有数据库文件可直接使用，无需数据迁移。

## 性能考虑

1. **连接池复用**：避免频繁创建/销毁连接
2. **批量操作**：对于大量数据插入，考虑使用 `session.add_all()`
3. **索引优化**：所有常用查询字段已添加索引
4. **软删除查询**：默认过滤 `deleted_time IS NULL`，已添加索引优化

## 后续改进建议

1. **Alembic 集成**：引入 Alembic 进行数据库版本管理
2. **查询优化**：对于复杂查询，考虑使用 SQLAlchemy 的 `select` 构建器
3. **缓存层**：对于频繁读取的数据，考虑添加 Redis 缓存
4. **监控**：添加数据库操作的性能监控和慢查询日志
