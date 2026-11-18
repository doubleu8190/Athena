# 本地 stdio MCP Server

此目录用于存放随 Athena 后端镜像构建和运行的管理员维护 MCP Server 源码。`payroll/` 是薪资闭环测试示例，不是默认生产业务 Server；删除或修改文件前应先核对服务器上已注册的 MCP 配置。

## 约定

- 实现仅使用 Python，入口由镜像内的绝对 Python 解释器 `/usr/local/bin/python` 启动。
- 不安装或引入额外依赖，只使用镜像中已安装的 Python 依赖和系统能力。启动时不得调用 `pip`、`uv`、`npx` 或从网络下载代码。
- 每个 Server 建议使用独立子目录，例如 `mcp_servers/example/server.py`。镜像构建后对应容器路径为 `/app/mcp_servers/example/server.py`。
- `mcp_servers/payroll/` 保留为本地和隔离环境的测试示例；对应测试与 Skill 源码仍位于 `backend/tests/payroll/`。
- Server 必须使用 stdin/stdout 传输 MCP 协议消息，并把诊断日志写到 stderr。stdout 不能输出日志或其他非协议内容。
- 不要把密码、Token 等秘密写入源码、命令或参数。stdio Server 的环境秘密通过管理 API 的 `secrets` 映射配置。

## 容器内注册

管理员注册 stdio Server 时，`command` 和可选的 `cwd` 必须是绝对路径。以 `example/server.py` 为例，管理 API 的连接配置应为：

```json
{
  "name": "example",
  "transport": "stdio",
  "command": "/usr/local/bin/python",
  "args": ["/app/mcp_servers/example/server.py"],
  "cwd": "/app/mcp_servers/example",
  "secrets": {}
}
```

脚本依赖同一目录中的模块时，建议设置绝对 `cwd`，或由 Server 自行基于 `__file__` 解析资源路径。Athena 不会通过 shell 展开变量、执行管道或拼接命令。

## 构建与部署

`docker/Dockerfile.backend` 会把仓库根目录的 `mcp_servers/` 复制到镜像的 `/app/mcp_servers/`。API、Run Worker 及其他后端服务共用 `athena-backend:latest`，因此源码变更必须重建镜像并重新创建相关容器：

```sh
docker compose build backend-api backend-worker
docker compose up -d --force-recreate backend-api backend-worker
```

如果部署了使用同一镜像的 `backend-memory-worker` 或 `backend-migration`，也应一并重建并重新创建。重建前确认所有目标 Server 的依赖已经由项目镜像提供；本目录不会在容器启动时安装依赖。

注册前可在容器内检查文件是否存在，以及解释器能否启动：

```sh
docker run --rm \
  --entrypoint /usr/local/bin/python \
  athena-backend:latest \
  -c "from pathlib import Path; assert Path('/app/mcp_servers/example/server.py').is_file()"
```

然后在管理 API 中注册并确认 `tools/list` 发现成功。运行中的容器不会感知宿主机目录变化，单改源码而不重建、重新创建容器不会更新正在执行的后端。
