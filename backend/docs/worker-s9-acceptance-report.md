# S9 生产集成验收切片

S9 新增的集成用例是 `tests/integration/test_worker_create_app_s9.py`。它调用
真实 `build_postgres_dependencies` 和 `bootstrap.create_app`，将连接通过
`PGOPTIONS` 限制到随机 PostgreSQL schema，并启动真实 HTTPS reference Worker
进程。用例验证健康路由、Cookie 登录、普通 API 的认证和 Origin 保护、Worker
Bearer/协议版本认证、Worker 注册写入 PostgreSQL，以及应用 HTTP 进程停止前
持久事实仍可读取。

用例只启动生产依赖图中的 `PostgresEngine`。Embedding、Neo4j、Sandbox、
LangGraph checkpointer 和后台消费者没有启动，因此这不是完整生产进程或真实
Root 执行链的证明。S9 本切片专门证明应用工厂、认证中间件、Worker 路由和
PostgreSQL wiring；S7/S8 的独立 Worker 执行与负载证据仍需结合阅读。

推荐命令（在 `backend` 目录执行）：

```bash
PYTHONPATH=src:.. ATHENA_TEST_DATABASE_URL='loopback PostgreSQL URL' \
  ../.venv/bin/python -m pytest -q tests/integration/test_worker_create_app_s9.py
```

测试使用随机 schema 并在 `finally` 中删除，不打印数据库 URL、密码、Bearer
token 或执行授权内容。
