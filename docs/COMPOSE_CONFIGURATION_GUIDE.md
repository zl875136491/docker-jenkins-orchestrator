# Docker Compose配置指南

## 快速自检清单

在创建构建前，请确保你的Compose配置符合以下要求：

### ✅ 配置规范

- [ ] **逐服务区分数据类型**
  - 配置文件 → Dockerfile `COPY`
  - 运行时数据 → 构建范围的命名卷
  - 密钥 → 运行时环境变量（不进镜像/Compose/bind mount）

- [ ] **Git源（默认）限制**
  - [ ] 不使用相对路径bind mount（如`./data:/app/data`）
  - [ ] 配置文件通过`COPY`指令放入镜像
  - [ ] 数据持久化使用命名卷

- [ ] **命名卷配置**
  - [ ] 不设置`external: true`（避免跨构建污染）
  - [ ] 不设置共享的`name`（如`name: global-cache`）
  - [ ] 理解cleanup会删除卷及其数据

- [ ] **生产数据存储**
  - [ ] 不依赖Docker卷存储生产数据
  - [ ] 使用外部数据库服务（RDS、云数据库）或对象存储（S3、OSS）
  - [ ] 实施备份/迁移策略

## 构建源类型

### Git源（默认）

**特点**：
- 文件通过Git仓库提供
- Jenkins阶段执行`git clone`获取代码
- Validating阶段**没有**源文件（workspace snapshot为空）

**适用场景**：
- 标准的应用程序开发流程
- 代码托管在Git仓库
- 需要版本控制和CI/CD集成

**Compose示例**：
```yaml
# x-orchestrator-build-source: git  # 可省略，默认为git

services:
  web:
    build: .  # ✅ Dockerfile在Git仓库
    image: my-app
    ports:
      - "8080:8080"
    volumes:
      - app-data:/var/lib/app  # ✅ 使用命名卷
    environment:
      DATABASE_URL: ${DATABASE_URL}  # ✅ 运行时注入

volumes:
  app-data:  # ✅ 构建托管卷
```

**Dockerfile示例**：
```dockerfile
FROM node:20-alpine

WORKDIR /app

# ✅ 配置文件通过COPY放入镜像
COPY package*.json ./
RUN npm ci --only=production

COPY src/ ./src/
COPY config/ ./config/

# ❌ 不要用bind mount挂载config/
# volumes: ["./config:/app/config"]  # Git源不支持

CMD ["node", "src/index.js"]
```

### Workspace源

**特点**：
- 文件通过Workspace API上传
- Validating阶段可以访问上传的文件
- 支持相对路径bind mount

**适用场景**：
- 快速原型开发
- 不使用Git的小型项目
- 需要动态替换配置文件

**Compose示例**：
```yaml
x-orchestrator-build-source: workspace  # ✅ 显式声明workspace源

services:
  web:
    build: .
    volumes:
      - ./static:/usr/share/nginx/html:ro  # ✅ workspace源支持bind mount
      - logs:/var/log/nginx  # ✅ 日志用命名卷

volumes:
  logs:
```

**上传文件**：
```bash
# 上传Dockerfile和静态文件
curl -X POST http://api/workspace/APP-Demo \
  -H "Authorization: Bearer $TOKEN" \
  -F "files=@Dockerfile" \
  -F "files=@static/index.html" \
  -F "files=@static/style.css"
```

## 卷配置详解

### 命名卷（推荐）

**构建托管卷**：
```yaml
services:
  postgres:
    image: postgres:16
    volumes:
      - pgdata:/var/lib/postgresql/data  # ✅ 简洁的卷引用

volumes:
  pgdata:  # ✅ 最小配置，由构建托管
    # 不设置driver、external、name等选项
```

**运行时行为**：
- 首次部署：自动创建卷（名称如`build-abc123-pgdata`）
- 更新部署：卷保留（同一个build_id）
- Cleanup：**自动删除**卷及其数据

**⚠️ 重要提示**：
```yaml
# ❌ 避免使用external（跨构建污染）
volumes:
  shared-cache:
    external: true
    name: global-cache  # 所有构建共享，数据污染风险

# ❌ 避免手动命名（可能冲突）
volumes:
  data:
    name: my-fixed-name  # 多个构建可能冲突
```

### Bind Mount

**Git源限制**：
```yaml
# ❌ Git源不支持相对bind mount
x-orchestrator-build-source: git
services:
  app:
    volumes:
      - ./config:/app/config  # ❌ validating阶段文件不存在
```

**错误消息**：
```json
{
  "code": "COMPOSE_CONFIG_ERROR",
  "subcode": "UNSUPPORTED_GIT_BIND_MOUNT",
  "remediation": "Replace bind mounts with Dockerfile COPY instructions..."
}
```

**修复方案**：
```dockerfile
# ✅ 使用Dockerfile COPY
FROM myapp:base
COPY config/ /app/config/  # 配置进入镜像
```

**Workspace源支持**：
```yaml
# ✅ Workspace源可以使用bind mount
x-orchestrator-build-source: workspace
services:
  app:
    volumes:
      - ./config:/app/config:ro  # ✅ 需要先上传config/
```

### 绝对路径（禁止）

```yaml
# ❌ 禁止使用宿主机绝对路径
services:
  app:
    volumes:
      - /srv/data:/app/data  # ❌ 安全隐患，绕过隔离
      - /etc/ssl:/etc/ssl:ro  # ❌ 访问宿主机系统文件
```

## 数据类型处理策略

### 1. 配置文件（静态）

**特征**：应用启动所需的配置，构建时确定

**推荐方式**：Dockerfile `COPY`
```dockerfile
FROM node:20-alpine
COPY config/app.json /app/config/app.json
COPY config/nginx.conf /etc/nginx/nginx.conf
```

**不推荐**：
```yaml
# ❌ 不要用bind mount（Git源不支持）
volumes:
  - ./config:/app/config
```

### 2. 应用数据（动态、可丢弃）

**特征**：缓存、临时文件、会话数据，丢失可接受

**推荐方式**：构建托管的命名卷
```yaml
services:
  redis:
    volumes:
      - redis-cache:/data  # ✅ cleanup会清理

volumes:
  redis-cache:  # 临时缓存，可丢弃
```

### 3. 数据库数据（持久化、不可丢弃）

**特征**：用户数据、订单记录、关键业务数据

**❌ 错误方式**：
```yaml
# ❌ 不要用Docker卷存储生产数据
services:
  postgres:
    volumes:
      - pgdata:/var/lib/postgresql/data  # cleanup会删除！

volumes:
  pgdata:  # ❌ cleanup时数据丢失
```

**✅ 正确方式**：

#### 选项A：外部托管数据库
```yaml
services:
  app:
    environment:
      DATABASE_URL: postgresql://rds.amazonaws.com:5432/prod  # ✅ AWS RDS
      # 或 DATABASE_URL: postgresql://postgres.example.com/prod  # ✅ 自建DB服务器
```

#### 选项B：备份/迁移机制
如果必须使用容器化数据库：
```yaml
services:
  postgres:
    volumes:
      - pgdata:/var/lib/postgresql/data

volumes:
  pgdata:  # ⚠️ 需要手动备份

# 部署前备份
# pg_dump -h old-build-postgres -U user db > backup.sql

# 新构建恢复
# psql -h new-build-postgres -U user db < backup.sql
```

**清理机制提醒**：
```bash
# 触发cleanup时
DELETE /api/v1/jenkins/builds/{build_id}

# 会删除：
# - 所有容器
# - 所有网络
# - 所有构建托管的卷（包括pgdata）
# - 本地缓存镜像
```

### 4. 密钥和凭据

**特征**：API密钥、数据库密码、证书

**推荐方式**：运行时环境变量
```yaml
services:
  app:
    environment:
      SECRET_KEY: ${SECRET_KEY}  # ✅ 运行时注入
      DATABASE_PASSWORD: ${DB_PASSWORD}  # ✅ 不进镜像
```

**平台注入**：
- 通过App环境变量设置
- 运行时从安全存储获取
- 不出现在镜像层、Compose文件、Git仓库

**❌ 禁止方式**：
```dockerfile
# ❌ 不要把密钥写入镜像
ENV SECRET_KEY=hardcoded-secret
COPY secrets.json /app/
```

```yaml
# ❌ 不要通过bind mount挂载密钥
volumes:
  - ./secrets:/app/secrets  # Git源不支持，且不安全
```

## 常见场景配置

### Node.js Web应用（Git源）

```yaml
services:
  web:
    build:
      context: .
      dockerfile: Dockerfile
    image: myapp
    ports:
      - "3000:3000"
    environment:
      NODE_ENV: production
      DATABASE_URL: ${DATABASE_URL}
      REDIS_URL: ${REDIS_URL}
    volumes:
      - logs:/var/log/app  # ✅ 日志卷
    depends_on:
      - redis

  redis:
    image: redis:7.4-alpine
    volumes:
      - redis-data:/data  # ✅ Redis持久化

volumes:
  logs:
  redis-data:
```

**Dockerfile**：
```dockerfile
FROM node:20-alpine

WORKDIR /app

# 依赖层
COPY package*.json ./
RUN npm ci --only=production

# 源码层
COPY src/ ./src/
COPY public/ ./public/

# ✅ 配置文件进镜像
COPY config/production.json ./config/

EXPOSE 3000
CMD ["node", "src/server.js"]
```

### Python应用 + PostgreSQL（Git源）

```yaml
services:
  app:
    build: .
    image: myapp
    ports:
      - "8000:8000"
    environment:
      DATABASE_URL: postgresql://postgres:5432/appdb
      SECRET_KEY: ${SECRET_KEY}
    depends_on:
      - db

  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: appdb
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - postgres-data:/var/lib/postgresql/data
      # ⚠️ 提醒：cleanup会删除此卷
      # 生产环境建议使用外部RDS

volumes:
  postgres-data:
```

**Dockerfile**：
```dockerfile
FROM python:3.12-slim

WORKDIR /app

# ✅ 先复制依赖文件（缓存层）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ✅ 复制应用代码
COPY app/ ./app/
COPY migrations/ ./migrations/

# ✅ 配置文件进镜像
COPY config/ ./config/

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Nginx静态站点（Workspace源）

```yaml
x-orchestrator-build-source: workspace

services:
  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
    volumes:
      - ./html:/usr/share/nginx/html:ro  # ✅ workspace源支持
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - logs:/var/log/nginx

volumes:
  logs:
```

**上传文件**：
```bash
curl -X POST http://api/workspace/APP-Site \
  -H "Authorization: Bearer $TOKEN" \
  -F "files=@html/index.html" \
  -F "files=@html/style.css" \
  -F "files=@html/script.js" \
  -F "files=@nginx.conf"
```

### 多服务微服务（Git源）

```yaml
services:
  frontend:
    build:
      context: ./frontend
    image: myapp-frontend
    ports:
      - "3000:80"

  api:
    build:
      context: ./api
    image: myapp-api
    ports:
      - "8080:8080"
    environment:
      DATABASE_URL: ${DATABASE_URL}
      REDIS_URL: redis://cache:6379
    depends_on:
      - cache

  cache:
    image: redis:7.4-alpine
    volumes:
      - redis-cache:/data

  worker:
    build:
      context: ./worker
    image: myapp-worker
    environment:
      REDIS_URL: redis://cache:6379
      QUEUE_NAME: tasks
    depends_on:
      - cache

volumes:
  redis-cache:
```

## 配置验证

### 提交前自检

运行以下检查确保配置正确：

```bash
# 1. 语法检查
docker compose -f docker-compose.yml config

# 2. 检查相对路径bind mount（Git源）
grep -r "^\s*-\s*\\./" docker-compose.yml && echo "⚠️  发现相对bind mount，Git源不支持"

# 3. 检查external卷
grep -A 1 "volumes:" docker-compose.yml | grep "external: true" && echo "⚠️  发现external卷，可能污染"

# 4. 检查绝对路径
grep -r "^\s*-\s*/[^:]" docker-compose.yml && echo "⚠️  发现绝对路径bind mount"
```

### 常见配置错误

| 错误配置 | 症状 | 修复方案 |
|---------|------|----------|
| Git源用`./config:/app/config` | `UNSUPPORTED_GIT_BIND_MOUNT` | Dockerfile `COPY config/ /app/config/` |
| `volumes: {data: {external: true}}` | 跨构建数据污染 | 移除`external`，使用构建托管卷 |
| 生产数据在Docker卷 | cleanup时数据丢失 | 使用外部数据库服务 |
| 密钥在镜像层 | 安全泄露 | 使用运行时环境变量 |
| workspace源忘记上传文件 | `does not exist; upload it` | 先上传文件再创建构建 |

## 更新与迁移

### 跨版本数据迁移

**场景**：从build-v1迁移到build-v2

**❌ 错误假设**：
```
# 卷名称相同，数据会自动保留？
volumes:
  pgdata:  # ❌ 每个构建有独立的卷前缀
```

**✅ 正确流程**：

#### 1. 备份旧构建数据
```bash
# 连接到旧构建的数据库容器
docker exec build-v1-db pg_dump -U user db > backup-v1.sql

# 或导出卷内容
docker run --rm -v build-v1-pgdata:/data -v $(pwd):/backup \
  alpine tar czf /backup/data-v1.tar.gz /data
```

#### 2. 部署新构建
```bash
# 创建新构建（会有新的卷前缀 build-v2-*）
POST /api/v1/jenkins/APP-Demo/builds
```

#### 3. 恢复数据到新构建
```bash
# 等待新构建部署完成
# 恢复数据库
docker exec -i build-v2-db psql -U user db < backup-v1.sql

# 或恢复卷内容
docker run --rm -v build-v2-pgdata:/data -v $(pwd):/backup \
  alpine tar xzf /backup/data-v1.tar.gz -C /
```

#### 4. 验证并清理旧构建
```bash
# 验证新构建数据完整
# 确认无误后清理旧构建（会删除旧卷）
DELETE /api/v1/jenkins/builds/build-v1-id
```

## 限制与能力说明

以下说明基于源码实际能力，不夸大功能。

### 支持的Compose功能

- ✅ `services`: 服务定义（build、image、ports、environment、volumes等）
- ✅ `volumes`: 命名卷（构建托管）
- ✅ `networks`: 自定义网络（build前缀隔离）
- ✅ `depends_on`: 启动顺序依赖
- ✅ `environment`: 环境变量（支持平台注入`${VAR}`）
- ✅ `build`: 构建上下文（相对路径，Git仓库内）
- ✅ `ports`: 端口映射（动态分配避免冲突）

### 不支持的Compose功能

- ❌ `extends`: 服务继承（Docker Compose扩展特性）
- ❌ `profiles`: 服务配置文件选择
- ❌ `configs`, `secrets`: Swarm/Kubernetes特性（使用环境变量替代）
- ❌ `deploy`: Swarm部署配置（使用Swarm但不完全支持所有选项）

### Git源特定限制

- ❌ 相对路径bind mount（`./path:/target`）
  - 原因：validating阶段文件未checkout
  - 替代：Dockerfile `COPY`

- ❌ `build.context`指向Git仓库外
  - 原因：构建上下文限于克隆的仓库
  - 替代：单一仓库或Git submodules

### Workspace源特定限制

- ❌ 超过配额的上传（默认100MB/10000文件）
  - 解决：使用Git源或增加配额
  
- ❌ 动态生成的大量文件
  - 解决：使用Git源，构建时生成

### 通用限制

- ❌ 宿主机绝对路径bind mount（安全隔离）
- ❌ 直接访问`/srv/workspaces/...`托管路径
- ❌ 跨构建共享卷（external: true）污染风险
- ❌ 卷数据自动跨版本保留（需手动迁移）

## 相关文档

- [Compose错误分类](./COMPOSE_ERROR_CLASSIFICATION.md) - 错误代码详解
- [构建清理机制](./BUILD_CLEANUP.md) - Cleanup行为说明
- [Workspace API](./WORKSPACE_API.md) - 文件上传接口

## 技术支持

遇到配置问题？

1. **检查构建日志**：`GET /api/v1/jenkins/builds/{build_id}`
2. **查看error_detail**：包含`code`、`remediation`等结构化信息
3. **参考本指南**：快速自检清单和常见场景
4. **联系技术支持**：提供`build_id`和完整的Compose配置（脱敏）

---

**最后更新**: 2026-11-17
**版本**: 2.0（结构化错误分类）
