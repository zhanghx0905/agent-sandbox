# 🐳 Agent Sandbox — 多租户持久化 Python 代码沙箱（Docker + MinIO/S3）

> 专为 **ChatGPT-like 会话体验** 设计的高性能、安全、可扩展的沙箱系统，支持变量跨运行持久化、S3 自动同步、租户隔离与自动回收。


## 🚀 核心特性

✅ **多租户隔离**：每个 `(user_id, session_id)` 对拥有独立的沙箱容器与工作区  
✅ **会话持久化**：基于 Python daemon (`__sandboxd__.py`) 保留变量状态，支持 REPL 风格交互  
✅ **自动创建/重连**：无需 `/init`，首次调用即自动创建沙箱，重启服务后自动重连  
✅ **增量 S3 同步**：仅上传变更文件，高效节省带宽与存储  
✅ **容器回收机制**：基于 TTL 与闲置超时自动清理过期沙箱（Reaper）  
✅ **安全强化**：SSRF 防护（默认禁止私有 IP）、路径白名单、只读容器、无特权  
✅ **批量上传支持**：通过 URL 批量上传文件到沙箱（支持 Content-Disposition 智能命名）  

---

## 📦 架构概览

```
[Client] → [API Server] → [Sandbox Manager] → [Docker Container (Python 3.12)]  
                             ↓  
                      [S3/MinIO] ← (增量同步)
```

- 每个沙箱 = 1 个 Docker 容器 + 1 个本地工作目录 + 1 个 S3 前缀  
- 所有容器由 `__sandboxd__.py` 守护进程提供执行服务  
- 容器通过 Unix Socket 接收代码执行请求，支持 `exec`, `reset`, `ping`  
- 客户端通过 `__sb_client__.py` 调用守护进程（支持超时、输出截断）

---

## ⚙️ 环境变量配置（`.env` 示例）

```env
# 🔐 认证
SANDBOX_API_KEY=your-secret-api-key

# ☁️ S3/MinIO 配置
S3_ENDPOINT_URL=http://127.0.0.1:9000
S3_ACCESS_KEY_ID=minioadmin
S3_SECRET_ACCESS_KEY=minioadmin
S3_BUCKET=sandbox
S3_REGION=us-east-1
S3_PREFIX_ROOT=sandboxes
PRESIGN_EXPIRES_SECONDS=300

# 🐳 Docker 沙箱配置
SANDBOX_IMAGE=python:3.12-slim
SANDBOX_TTL_SECONDS=1800       # 最长生命周期（30分钟）
SANDBOX_IDLE_SECONDS=600       # 最大空闲时间（10分钟）
SANDBOX_EXEC_TIMEOUT_SECONDS=10 # 单次执行超时
SANDBOX_REAPER_INTERVAL=15     # 回收器轮询间隔（秒）
SANDBOX_MAX_TOTAL=50           # 最大同时沙箱数

# 🗃️ 存储路径
WORK_ROOT=./sandboxes

# 📤 上传/输出限制
MAX_UPLOAD_BYTES=52428800      # 最大上传 50MB
MAX_OUTPUT_BYTES=200000        # 最大输出 200KB

# 🧹 清理行为
DELETE_S3_ON_CLEANUP=1         # 清理沙箱时删除 S3 前缀（默认）
DELETE_S3_ON_DELETE=0          # 删除本地文件时是否同步删除 S3 文件（默认否）

# 🌐 SSRF 防护
ALLOW_PRIVATE_URLS=0           # 禁止下载私有 IP URL（默认）
```

---

## 📡 API 端点

### ✅ `/v1/execute` — 执行 Python 代码（推荐）

```json
POST /v1/execute
Headers: X-API-Key: your-secret-api-key

Body:
{
  "user_id": "alice",
  "session_id": "session123",
  "code": "x = 5\nprint(x * 2)\nx",
  "inputs": ["/path/to/input1.py", "/path/to/input2.txt"],
  "presign_expires_in": 300
}
```

**响应示例：**

```json
{
  "output": "10\n5\n",
  "artifacts": [
    {
      "path": "output.txt",
      "size_bytes": 12,
      "mime_type": "text/plain",
      "download_url": "https://s3.example.com/...?Expires=...&Signature=..."
    }
  ]
}
```

> ✨ 最后一行表达式会像 REPL 一样自动 `print(repr(...))`

---

### 🔄 `/v1/sandbox/reset` — 重置内核（清空变量，保留文件）

```json
POST /v1/sandbox/reset
{
  "user_id": "alice",
  "session_id": "session123"
}
```

**响应：**

```json
{
  "status": "success",
  "output": "Kernel reset.\n",
  "container_id": "abc123..."
}
```

---

### 🚫 `/v1/sandbox/stop` — 彻底销毁沙箱

```json
POST /v1/sandbox/stop
{
  "user_id": "alice",
  "session_id": "session123"
}
```

> ✅ 自动删除容器、本地目录、S3 前缀（如 `DELETE_S3_ON_CLEANUP=1`）

---

### 📤 `/v1/files/upload-urls` — 批量上传文件（通过 URL）

```json
POST /v1/files/upload-urls
{
  "user_id": "alice",
  "session_id": "session123",
  "urls": [
    "https://example.com/data.csv",
    "https://example.com/image.jpg?Content-Disposition=filename%3D%22myimage.jpg%22"
  ],
  "dest_dir": "data"  // 可选：上传到沙箱内的子目录
}
```

**响应示例：**

```json
{
  "status": "partial_success",
  "uploaded": ["data/data.csv", "data/myimage.jpg"],
  "errors": [
    { "source_url": "https://bad.url", "error": "Connection timeout" }
  ]
}
```

---

### 📋 `/v1/files/list` — 列出沙箱内所有文件

```json
GET /v1/files/list?user_id=alice&session_id=session123
```

```json
{
  "files": [
    { "path": "script.py", "size_bytes": 120, "mtime": 1712345678 },
    { "path": "output.txt", "size_bytes": 12, "mtime": 1712345680 }
  ]
}
```

---

### 📥 `/v1/files/download-url` — 获取文件下载链接

```json
GET /v1/files/download-url?user_id=alice&session_id=session123&path=script.py&expires_in=600
```

```json
{
  "status": "success",
  "path": "script.py",
  "expires_in": 600,
  "download_url": "https://s3.example.com/...?Expires=...&Signature=..."
}
```

---

## 🛡️ 安全特性

- **API 密钥认证**：必须携带 `X-API-Key` 头部  
- **路径安全**：禁止 `..` 和绝对路径，仅允许相对路径  
- **SSRF 防护**：默认禁止下载 `localhost`、`127.0.0.1`、私有 IP（可配置）  
- **容器安全**：
  - `read_only=True`
  - `no-new-privileges`
  - `cap_drop=["ALL"]`
  - `network_mode="none"`（默认无网络）
  - `tmpfs=/tmp` 限制临时文件系统
- **文件过滤**：内部文件（如 `__sandboxd__.py`）不暴露于列表或下载

---

## 🧰 快速部署

### 1. 安装依赖

```bash
pip install fastapi uvicorn docker boto3 botocore pydantic python-multipart python-dotenv
```

### 2. 启动 MinIO（可选，如使用 S3）

```bash
docker run -d -p 9000:9000 -p 9001:9001 \
  -e "MINIO_ROOT_USER=minioadmin" \
  -e "MINIO_ROOT_PASSWORD=minioadmin" \
  minio/minio server /data --console-address ":9001"
```

> Web 控制台: `http://localhost:9001` → 创建桶 `sandbox`

### 3. 启动服务

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### 4. 测试健康检查

```bash
curl http://localhost:8000/health
# {"status": "ok"}
```

---

## 🔄 生命周期管理

### 沙箱状态

- **创建**：首次调用 `/execute` 时自动创建容器与工作目录  
- **重连**：服务重启后，基于 `container_name = sb_{user_key}_{session_key}` 重连现有容器  
- **回收**：
  - **内存沙箱**：过期自动从内存移除并清理
  - **Docker 孤儿**：Reaper 定期扫描 `sandbox.managed=1` 标签容器，根据本地 `.sb_meta.json` 判断过期并清理
- **强制清理**：调用 `/stop` 或 `DELETE_S3_ON_CLEANUP=1` 时删除 S3 数据

---

## 📈 性能与限制

| 项目                  | 说明                                 |
|-----------------------|--------------------------------------|
| 并发上限              | `SANDBOX_MAX_TOTAL`（默认 50）       |
| 单次执行超时          | `SANDBOX_EXEC_TIMEOUT_SECONDS`（10s）|
| 输出截断              | 超过 `MAX_OUTPUT_BYTES` 自动截断     |
| 上传单文件大小限制    | `MAX_UPLOAD_BYTES`（默认 50MB）      |
| 批量上传 URL 数量限制 | 最多 200 个                          |
| Presigned URL 有效期  | 最大 7 天（604800 秒）               |

---

## 🧪 示例：模拟 ChatGPT 会话

```python
# 第一次执行：定义变量
code = "x = 10\nx"

# 输出：10

# 第二次执行：使用上一次变量
code = "x += 5\nx"

# 输出：15
```

> 变量 `x` 在同一 `session_id` 下自动保留！
