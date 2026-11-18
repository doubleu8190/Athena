# 月度薪资测试夹具与 MCP Server

该目录保留薪资闭环测试和可导入的 Athena Skill 包；带七类只读薪资模拟数据的
stdio MCP Server 源码位于仓库根目录 `mcp_servers/payroll/`。Server 直接读取内置
数据，不启动或连接 HTTP 上游，整套夹具只用于本地或隔离测试环境。

## 组件

- `mcp_servers/payroll/mcp_server.py`：内置固定 `2026-11` 的员工、部门任职、基本工资、无薪假、奖金、年假余额和离职数据，通过 stdio MCP 暴露 `payroll_query`。
- `skill_package/`：符合 Athena 管理员 Skill ZIP 格式，入口为 `SKILL.md`，脚本为 `scripts/build_payroll.py`。
- `test_payroll_skill.py`：直接启动真实 stdio MCP 子进程，验证内置数据、Skill 包和真实脚本生成的 Excel。

E005 故意缺少月基本工资。核算 D500 时应只包含 E004 和 E005：E004 成功、应付
`3420.00`；E005 失败、不进入总额。D100、D200、D300、D400 的员工和明细不得出现在
D500 报告中。其他部门分别核算时也应只返回本部门任职记录确定的员工。

## 本地部署

无需启动上游服务。在 Athena 管理端直接注册 stdio MCP Server，命令和参数使用绝对
路径；启动参数只包含脚本路径，不传 `--base-url`：

```json
{
  "name": "Payroll Test",
  "transport": "stdio",
  "command": "/Users/udouble/Documents/code/Athena_v2/.venv/bin/python",
  "args": [
    "/Users/udouble/Documents/code/Athena_v2/mcp_servers/payroll/mcp_server.py"
  ],
  "cwd": "/Users/udouble/Documents/code/Athena_v2/mcp_servers/payroll",
  "secrets": {}
}
```

对应接口为 `POST /api/v1/admin/mcp/servers`。注册后确认工具目录中出现
`payroll_query`。

后端镜像会把 `mcp_servers/` 复制到 `/app/mcp_servers/`；重新构建后，Server 的镜像路径为
`/app/mcp_servers/payroll/mcp_server.py`。若需要重新挂载测试夹具，应把新的源码目录
只读挂载到 API 与 Worker 共享的 `/app/var/payroll-test`。Compose 的 `athena-var`
卷已同时挂载到 `backend-api` 和 `backend-worker` 的 `/app/var`，可使用如下 override：

```yaml
services:
  backend-api:
    volumes:
      - ./mcp_servers/payroll:/app/var/payroll-test:ro
  backend-worker:
    volumes:
      - ./mcp_servers/payroll:/app/var/payroll-test:ro
```

如果不用 Compose，也必须把同一宿主目录分别挂载到两个容器的
`/app/var/payroll-test:ro`。只挂载到一个容器时，API 可以发现 MCP，但 Run Worker 仍无法执行。

容器注册内容示例：

```json
{
  "name": "Payroll Test",
  "transport": "stdio",
  "command": "/usr/local/bin/python",
  "args": [
    "/app/var/payroll-test/mcp_server.py"
  ],
  "cwd": "/app/var/payroll-test",
  "secrets": {}
}
```

重建镜像并重新创建容器后，也可把注册配置的脚本路径和 `cwd` 改为
`/app/mcp_servers/payroll` 下的绝对路径；两种路径都不需要额外上游参数。

打包 Skill 时，ZIP 根目录必须直接包含 `SKILL.md`，不能多套一层目录：

```sh
cd backend/tests/payroll/skill_package
rm -f /tmp/monthly-payroll-skill.zip
zip -r /tmp/monthly-payroll-skill.zip SKILL.md scripts references \
  -x '*/__pycache__/*' '*.pyc'
unzip -l /tmp/monthly-payroll-skill.zip
```

不要使用 `python -m zipfile -c`：该命令只保留输入文件的 basename，会把
`scripts/build_payroll.py` 和 `references/payroll-rules.md` 压成根目录下的
`build_payroll.py`、`payroll-rules.md`，导致 manifest 中的脚本路径失效。`zip -r`
会保留相对目录，上一条 `unzip -l` 应显示 `scripts/build_payroll.py` 和
`references/payroll-rules.md`。

管理员通过 `POST /api/v1/admin/skills/import` 上传该 ZIP，然后启用技能并选择
`scripts/build_payroll.py`。当前测试 Skill 依赖 `openpyxl>=3.1`；执行脚本的
Python 环境必须已安装该依赖。Docker 部署时应以
`ATHENA_INSTALL_KNOWLEDGE=true` 构建后端基础镜像，或在该镜像的
`ATHENA_SKILL_PYTHON_EXECUTABLE` 对应环境中单独安装 `openpyxl`。

## Docker 注意事项

Athena API 负责发现 stdio MCP，Run Worker 负责执行，因此 MCP 脚本必须同时存在于
两个容器的相同绝对路径，且两个容器中的命令解释器都必须能导入 `mcp`。Server
直接读取脚本内的模拟数据，不需要开放或访问额外端口。

执行顺序为：

1. 调用 `payroll_query`，月份传 `2026-11`。
2. 将用户指定的 `department_id` 作为 Skill 顶层参数，例如 `D500`。
3. 将 `structuredContent` 原样作为 `payroll_inputs`，执行 Skill 的
   `scripts/build_payroll.py`。
4. 从交付文件中使用 `session_file_read` 读取
   `payroll-2026-11-failures.txt`，下载 `payroll-2026-11.xlsx`，并告知用户 E005 的
   编号、姓名和失败原因。Skill 的 stdout 仅供管理员日志排查，不能替代摘要产物。

目标部门没有任职记录时，脚本以 `department_not_found` 失败，不会生成包含其他部门的
空报告。

## 验证

```sh
.venv/bin/python -m pytest -q backend/tests/payroll
```
