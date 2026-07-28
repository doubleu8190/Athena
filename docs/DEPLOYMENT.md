# Athena Docker 部署指南

## 目录

1. [部署架构](#部署架构)
2. [环境要求](#环境要求)
3. [快速开始](#快速开始)
4. [环境变量配置](#环境变量配置)
5. [Docker Compose 部署](#docker-compose-部署)
6. [前端部署](#前端部署)
7. [Electron 应用部署](#electron-应用部署)
8. [健康检查](#健康检查)
9. [日志管理](#日志管理)
10. [水平扩展](#水平扩展)
11. [数据持久化](#数据持久化)
12. [安全最佳实践](#安全最佳实践)
13. [故障排查](#故障排查)

---

## 部署架构

Athena 采用微服务架构，通过 Docker Compose 进行容器编排：

```
┌─────────────────────────────────────────────────────────────────┐
│                        客户端层                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐    │
│  │   Web UI    │  │  Electron   │  │  Telegram / WeChat  │    │
│  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘    │
└─────────┼────────────────┼─────────────────────┼───────────────┘
          │                │                     │
          ▼                ▼                     ▼
┌─────────────────────────────────────────────────────────────────┐
│                        网关层 (Nginx)                            │
│                        (开发环境)                                │
└─────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────┐
│                        应用层                                    │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐    │
│  │  athena-core│  │ arq-worker  │  │    skill-proxy      │    │
│  │   (API)     │  │ (异步任务)   │  │    (Squid代理)      │    │
│  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘    │
└─────────┼────────────────┼─────────────────────┼───────────────┘
          │                │                     │
          ▼                ▼                     ▼
┌─────────────────────────────────────────────────────────────────┐
│                        基础设施层                                 │
│  ┌─────────────┐                                                │
│  │    Redis    │                                                │
│  │ (缓存/队列) │                                                │
│  └─────────────┘                                                │
└─────────────────────────────────────────────────────────────────┘
```

### 服务清单

| 服务 | 镜像 | 端口 | 描述 |
|------|------|------|------|
| redis | redis:7-alpine | 6379 | 缓存、ARQ 任务代理 |
| athena-core | 自定义构建 | 8000 | FastAPI 后端服务 |
| arq-worker | 自定义构建 | - | 异步任务执行 |
| skill-proxy | ubuntu/squid | 3128 | Skill 网络隔离代理 |

---

## 环境要求

- Docker >= 24.0
- Docker Compose >= 2.20
- 内存 >= 8GB (推荐 16GB)
- CPU >= 4核

---

## 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/udouble/Athena.git
cd Athena
```

### 2. 创建环境变量文件

```bash
cp .env.example .env
```

### 3. 创建密钥文件

```bash
# 创建 secrets 目录（如果不存在）
mkdir -p secrets

# 创建必需的密钥文件
echo "your-admin-api-key" > secrets/admin_api_key
echo "your-encryption-key" > secrets/athena_encryption_key
echo "your-device-psk" > secrets/device_psk

# 创建可选的密钥文件（根据需要）
echo "your-telegram-bot-token" > secrets/telegram_bot_token
echo "your-wechat-bot-token" > secrets/wechat_bot_token
echo "your-wechat-ilink-bot-id" > secrets/wechat_ilink_bot_id
echo "your-mimo-api-key" > secrets/mimo_api_key
echo "your-deepseek-api-key" > secrets/deepseek_api_key

# 设置权限
chmod 600 secrets/*
```

### 4. 构建基础镜像

```bash
sh scripts/build-base.sh
```

### 5. 启动服务

```bash
# 开发环境（包含前端和网关）
docker compose up -d --build

# 生产环境（仅后端服务）
docker compose -f docker-compose.prod.yml up -d --build
```

### 6. 验证服务

```bash
# 检查健康状态
curl http://localhost:8000/api/v1/health

# 预期输出
{"status":"healthy","version":"0.1.0"}
```

---

## 环境变量配置

### 核心配置

| 变量 | 类型 | 默认值 | 描述 |
|------|------|--------|------|
| `LOG_LEVEL` | string | INFO | 日志级别：DEBUG, INFO, WARNING, ERROR |
| `UVICORN_WORKERS` | int | 4 | Uvicorn 工作进程数 |
| `ATHENA_CORE_REPLICAS` | int | 1 | athena-core 副本数 |
| `ARQ_WORKER_REPLICAS` | int | 2 | ARQ Worker 副本数 |
| `CORS_ORIGINS` | string | * | CORS 允许的源 |

### 数据库配置

| 变量 | 类型 | 默认值 | 描述 |
|------|------|--------|------|
| `SQLITE_DB_PATH` | string | /data/athena.db | SQLite 数据库路径 |

### Redis 配置

| 变量 | 类型 | 默认值 | 描述 |
|------|------|--------|------|
| `REDIS_URL` | string | redis://redis:6379/0 | Redis 连接 URL |
| `ARQ_BROKER_URL` | string | redis://redis:6379/1 | ARQ Broker URL |

### 网关配置

| 变量 | 类型 | 默认值 | 描述 |
|------|------|--------|------|
| `TELEGRAM_POLL_TIMEOUT` | int | 30 | Telegram 轮询超时（秒） |

### 监控配置

| 变量 | 类型 | 默认值 | 描述 |
|------|------|--------|------|
| `PROMETHEUS_ENABLED` | bool | true | 是否启用 Prometheus |

### 前端配置

| 变量 | 类型 | 默认值 | 描述 |
|------|------|--------|------|
| `VITE_API_BASE_URL` | string | /api/v1 | API 基础 URL |

---

## Docker Compose 部署

### 开发环境

开发环境包含完整的前后端服务和 ELK 日志系统：

```bash
# 启动所有服务
docker compose up -d --build

# 查看日志
docker compose logs -f

# 停止服务
docker compose down

# 停止并清理数据卷
docker compose down -v
```

### 生产环境

生产环境仅部署后端服务，前端和 Electron 应用独立部署：

```bash
# 启动生产环境
docker compose -f docker-compose.prod.yml up -d --build

# 查看特定服务日志
docker compose -f docker-compose.prod.yml logs -f athena-core

# 停止生产环境
docker compose -f docker-compose.prod.yml down
```

### 服务扩展

```bash
# 扩展 athena-core 到 3 个副本
docker compose -f docker-compose.prod.yml up -d --scale athena-core=3

# 扩展 arq-worker 到 4 个副本
docker compose -f docker-compose.prod.yml up -d --scale arq-worker=4
```

---

## 前端部署

### Web 前端

Web 前端可以独立部署在 Nginx、CDN 或云平台上：

#### 构建前端

```bash
cd frontend
npm install
VITE_API_BASE_URL=http://your-backend-host/api/v1 npm run build
```

#### 配置 API 地址

在 `.env` 文件中设置：

```env
VITE_API_BASE_URL=http://your-backend-host/api/v1
```

#### Nginx 部署

```nginx
server {
    listen 80;
    server_name your-domain.com;

    root /path/to/frontend/dist;
    index index.html;

    # SPA 路由支持
    location / {
        try_files $uri $uri/ /index.html;
    }

    # API 代理
    location /api/ {
        proxy_pass http://your-backend-host:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

---

## Electron 应用部署

### 开发模式

开发模式下，Electron 应用连接本地开发服务器：

```bash
# 启动后端服务
./start.sh backend

# 启动 Electron
cd electron
npm run dev
```

### 生产模式

生产模式下，Electron 应用使用内置的渲染进程：

#### 配置 API 地址

在 `electron/renderer/.env` 文件中设置：

```env
VITE_API_BASE_URL=http://your-backend-host/api/v1
```

#### 打包应用

```bash
cd electron
npm run build
npm run package:mac    # macOS
npm run package:win    # Windows
npm run package:linux  # Linux
```

#### 分发方式

- **macOS**: DMG 安装包或签名后的应用
- **Windows**: NSIS 安装程序
- **Linux**: AppImage 或 Debian 包

---

## 健康检查

### 后端健康检查

```bash
# 检查后端状态
curl http://localhost:8000/api/v1/health

# 检查 Prometheus 指标
curl http://localhost:8000/metrics
```

### Docker 健康检查

Docker Compose 配置了自动健康检查：

```yaml
healthcheck:
  test: ["CMD-SHELL", "curl -sf http://localhost:8000/api/v1/health || exit 1"]
  interval: 10s
  timeout: 5s
  retries: 5
  start_period: 30s
```

### 查看健康状态

```bash
# 查看所有服务健康状态
docker compose ps

# 查看特定服务健康状态
docker inspect --format='{{.State.Health.Status}}' athena-athena-core-1
```

---

## 日志管理

### Docker 原生日志

Athena 使用 Docker 原生日志驱动进行日志管理：

- **json-file 驱动**: 默认启用，自动轮转
- **日志保留**: 每个文件最大 10MB，保留 3-5 个文件

### 查看日志

```bash
# 查看所有服务日志
docker compose logs

# 查看特定服务日志
docker compose logs athena-core

# 实时跟踪日志
docker compose logs -f athena-core

# 查看最近的日志
docker compose logs --tail=100 athena-core
```

### 日志级别调整

```bash
# 通过环境变量设置
LOG_LEVEL=DEBUG docker compose up -d
```

### 日志轮转配置

```yaml
logging:
  driver: json-file
  options:
    max-size: "10m"
    max-file: "5"
```

---

## 水平扩展

### Docker Compose 扩展

```bash
# 扩展服务
docker compose -f docker-compose.prod.yml up -d \
  --scale athena-core=3 \
  --scale arq-worker=4

# 查看副本状态
docker compose -f docker-compose.prod.yml ps
```

### 负载均衡

在生产环境中，建议使用 Nginx 或其他负载均衡器：

```nginx
upstream athena-core {
    server athena-core-1:8000;
    server athena-core-2:8000;
    server athena-core-3:8000;
}

server {
    listen 80;

    location /api/ {
        proxy_pass http://athena-core;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

### 自动扩展

可以使用 Docker Swarm 实现自动扩缩容：

```yaml
# Docker Swarm 自动扩展配置
deploy:
  replicas: 3
  update_config:
    parallelism: 1
    delay: 30s
  restart_policy:
    condition: any
  resources:
    limits:
      memory: 2G
    reservations:
      memory: 1G
```

---

## 数据持久化

### Docker Volumes

项目使用 Docker Volumes 进行数据持久化：

| Volume | 用途 |
|--------|------|
| `redis-data` | Redis 数据 |
| `athena-data` | SQLite 数据库、配置文件、模型缓存 |
| `squid-data` | Squid 缓存 |
| `es-data` | Elasticsearch 索引 |

### 数据备份

```bash
# 备份所有数据卷
docker run --rm -v athena-data:/data -v $(pwd)/backup:/backup \
  busybox tar -czf /backup/athena-data-$(date +%Y%m%d).tar.gz /data

# 备份 Redis
docker exec athena-redis-1 redis-cli SAVE
docker cp athena-redis-1:/data/dump.rdb ./backup/redis-dump-$(date +%Y%m%d).rdb
```

### 数据恢复

```bash
# 恢复数据卷
docker run --rm -v athena-data:/data -v $(pwd)/backup:/backup \
  busybox tar -xzf /backup/athena-data-20240101.tar.gz -C /

# 恢复 Redis
docker cp ./backup/redis-dump-20240101.rdb athena-redis-1:/data/dump.rdb
docker restart athena-redis-1
```

---

## 安全最佳实践

### 密钥管理

- 使用 Docker Secrets 管理敏感信息
- 不要将密钥提交到版本控制系统
- 设置密钥文件权限为 `600`

### 网络隔离

- 使用 Docker 网络隔离不同服务
- Skill 容器使用 `network_mode: none` 禁止外连
- 通过 Squid 代理实现最小权限网络访问

### HTTPS

在生产环境中启用 HTTPS：

```bash
# 使用 Let's Encrypt
certbot certonly --nginx -d your-domain.com
```

### 防火墙

```bash
# 只开放必要端口
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable
```

---

## 故障排查

### 常见问题

#### 1. Redis 连接失败

```bash
# 检查 Redis 状态
docker compose ps redis

# 检查 Redis 日志
docker compose logs redis

# 测试 Redis 连接
docker exec -it athena-redis-1 redis-cli ping
```

#### 2. 后端服务启动失败

```bash
# 检查后端状态
docker compose ps athena-core

# 查看后端日志
docker compose logs athena-core

# 检查数据库连接
docker exec -it athena-athena-core-1 python -c "
import sqlite3
conn = sqlite3.connect('/data/athena.db')
print('Database connection OK')
conn.close()
"
```

#### 3. 前端无法访问 API

```bash
# 检查 CORS 配置
docker exec -it athena-athena-core-1 env | grep CORS_ORIGINS

# 测试 API 直接访问
curl http://localhost:8000/api/v1/health

# 检查 Nginx 代理配置
docker exec -it athena-athena-gateway-1 cat /etc/nginx/conf.d/default.conf
```

#### 4. 日志查看

```bash
# 查看服务日志
docker compose logs athena-core

# 实时跟踪日志
docker compose logs -f athena-core

# 查看最近的日志
docker compose logs --tail=200 athena-core
```

### 日志级别

```bash
# 临时设置 DEBUG 级别
LOG_LEVEL=DEBUG docker compose up -d athena-core

# 查看详细日志
docker compose logs -f --tail=100 athena-core
```

---

## 部署清单

- [ ] 安装 Docker 和 Docker Compose
- [ ] 创建 `.env` 文件
- [ ] 创建 `secrets/` 目录和密钥文件
- [ ] 构建基础镜像
- [ ] 启动服务
- [ ] 验证健康状态
- [ ] 配置前端 API 地址
- [ ] 配置 HTTPS（生产环境）
- [ ] 定期备份数据
