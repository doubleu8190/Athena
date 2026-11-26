# 本地 stdio MCP Server

此目录用于存放本地联调与测试/演示用的管理员维护 stdio MCP Server 源码（例如 `payroll/` 薪资闭环示例与 `hroa/` 企业 HR/OA 联调 Server）。生产后端镜像 `athena-backend:latest` 默认**不打包**本目录；在本地 Docker 测试栈中由 `docker/compose.test-mcp.override.yml` 以只读卷方式挂载到容器内 `/app/mcp_servers:ro`。删除或修改文件前应先核对测试环境中已注册的 MCP 配置。

## 约定

- 实现仅使用 Python，入口由镜像内的绝对 Python 解释器 `/usr/local/bin/python` 启动。
- 不安装或引入额外依赖，只使用镜像中已安装的 Python 依赖和系统能力。启动时不得调用 `pip`、`uv`、`npx` 或从网络下载代码。
- 每个 Server 建议使用独立子目录，例如 `mcp_servers/example/server.py`。在测试栈只读挂载后对应容器路径为 `/app/mcp_servers/example/server.py`。
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

## 测试栈挂载与部署

`docker/Dockerfile.backend` **不再将** `mcp_servers/` 打包进生产镜像 `athena-backend:latest`，以保持生产镜像干净最小化。通过 `scripts/test-docker.sh` 启动本地测试栈时，会自动叠加 `docker/compose.test-mcp.override.yml`，将仓库根目录的 `./mcp_servers` 以只读方式挂载到 `backend-api` 与 `backend-worker` 的 `/app/mcp_servers:ro`：

```sh
# 方式一：通过测试栈脚本自动加载 compose.test-mcp.override.yml
scripts/test-docker.sh up

# 方式二：手动叠加测试栈只读挂载扩展文件
docker compose --env-file docker/test.env \
  -f compose.yml -f docker/compose.test-mcp.override.yml \
  up -d backend-api backend-worker
```

使用只读挂载后，宿主机修改 `mcp_servers/` 源码会即时反映到容器内 `/app/mcp_servers`（无需重建后端镜像），但新增或修改目标 Server 前仍需确认其依赖已包含在 `athena-backend-base:latest` 镜像内。注册前可在运行中的容器内检查文件与解释器：

```sh
docker compose --env-file docker/test.env \
  -f compose.yml -f docker/compose.test-mcp.override.yml \
  exec -T backend-api \
  /usr/local/bin/python -c "from pathlib import Path; assert Path('/app/mcp_servers/example/server.py').is_file()"
```

若需在生产环境部署企业自研 stdio MCP Server，建议在独立部署配置中以只读卷挂载受控目录，或在企业定制镜像中显式拷贝经审核的 Server 文件；完成后通过管理 API 注册并确认 `tools/list` 发现成功。
