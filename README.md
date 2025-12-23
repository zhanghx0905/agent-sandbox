# 🐳 Agent Sandbox — Multi-Tenant Persistent Python Code Sandbox (Docker + MinIO/S3)

> A high-performance, secure, and scalable sandbox system designed specifically for **ChatGPT-like conversational experiences**, supporting cross-run variable persistence, automatic S3 synchronization, tenant isolation, and automatic resource cleanup.

[中文](./README.CN.md)

## Usage

Create an `.env` with your configuration. Then,

```sh
docker build -t python:3.11-sandbox sandbox-build
bash ctl.sh start
```

## 🚀 Core Features

✅ **Multi-Tenant Isolation**: Each `(user_id, session_id)` pair owns its dedicated sandbox container and workspace  
✅ **Session Persistence**: Maintains variable state via Python daemon (`__sandboxd__.py`), enabling REPL-style interaction  
✅ **Auto-Creation / Reconnection**: No `/init` needed — sandbox auto-created on first call; auto-reconnects after service restart  
✅ **Incremental S3 Sync**: Uploads only changed files, efficiently saving bandwidth and storage  
✅ **Container Reclamation**: Automatically cleans expired sandboxes based on TTL and idle timeout (via Reaper)  
✅ **Security Hardening**: SSRF protection (blocks private IPs by default), path allowlist, read-only containers, no privileges  
✅ **Bulk Upload Support**: Upload multiple files via URL to sandbox (supports Content-Disposition intelligent naming)

---

## 📦 Architecture Overview

```
[Client] → [API Server] → [Sandbox Manager] → [Docker Container (Python 3.12)]  
                             ↓  
                      [S3/MinIO] ← (Incremental Sync)
```

- Each sandbox = 1 Docker container + 1 local working directory + 1 S3 prefix  
- All containers managed by `__sandboxd__.py` daemon providing execution services  
- Containers accept code execution requests via Unix Socket, supporting `exec`, `reset`, `ping`  
- Clients communicate via `__sb_client__.py` (supports timeouts, output truncation)

---

## ⚙️ Environment Configuration (`.env` Example)

```env
# 🔐 Authentication
SANDBOX_API_KEY=your-secret-api-key

# ☁️ S3/MinIO Configuration
S3_ENDPOINT_URL=http://127.0.0.1:9000
S3_ACCESS_KEY_ID=minioadmin
S3_SECRET_ACCESS_KEY=minioadmin
S3_BUCKET=sandbox
S3_REGION=us-east-1
S3_PREFIX_ROOT=sandboxes
PRESIGN_EXPIRES_SECONDS=300

# 🐳 Docker Sandbox Configuration
SANDBOX_IMAGE=python:3.12-slim
SANDBOX_TTL_SECONDS=1800       # Max lifetime (30 minutes)
SANDBOX_IDLE_SECONDS=600       # Max idle time (10 minutes)
SANDBOX_EXEC_TIMEOUT_SECONDS=10 # Single execution timeout
SANDBOX_REAPER_INTERVAL=15     # Reaper polling interval (seconds)
SANDBOX_MAX_TOTAL=50           # Max simultaneous sandboxes

# 🗃️ Storage Paths
WORK_ROOT=./sandboxes

# 📤 Upload / Output Limits
MAX_UPLOAD_BYTES=52428800      # Max upload size: 50MB
MAX_OUTPUT_BYTES=200000        # Max output size: 200KB

# 🧹 Cleanup Behavior
DELETE_S3_ON_CLEANUP=1         # Delete S3 prefix when cleaning sandbox (default)
DELETE_S3_ON_DELETE=0          # Delete S3 files when deleting local files? (default: no)

# 🌐 SSRF Protection
ALLOW_PRIVATE_URLS=0           # Block private IP downloads (default)
```

---

## 📡 API Endpoints

### ✅ `/v1/execute` — Execute Python Code (Recommended)

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

**Response Example:**

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

> ✨ Final expression auto-printed via `print(repr(...))`, like a REPL

---

### 🔄 `/v1/sandbox/reset` — Reset Kernel (Clear variables, preserve files)

```json
POST /v1/sandbox/reset
{
  "user_id": "alice",
  "session_id": "session123"
}
```

**Response:**

```json
{
  "status": "success",
  "output": "Kernel reset.\n",
  "container_id": "abc123..."
}
```

---

### 🚫 `/v1/sandbox/stop` — Terminate Sandbox Completely

```json
POST /v1/sandbox/stop
{
  "user_id": "alice",
  "session_id": "session123"
}
```

> ✅ Automatically deletes container, local directory, and S3 prefix (if `DELETE_S3_ON_CLEANUP=1`)

---

### 📤 `/v1/files/upload-urls` — Batch Upload Files via URL

```json
POST /v1/files/upload-urls
{
  "user_id": "alice",
  "session_id": "session123",
  "urls": [
    "https://example.com/data.csv",
    "https://example.com/image.jpg?Content-Disposition=filename%3D%22myimage.jpg%22"
  ],
  "dest_dir": "data"  // Optional: upload into subdirectory within sandbox
}
```

**Response Example:**

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

### 📋 `/v1/files/list` — List All Files in Sandbox

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

### 📥 `/v1/files/download-url` — Get Pre-Signed Download URL

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

## 🛡️ Security Features

- **API Key Authentication**: Requires `X-API-Key` header  
- **Path Safety**: Blocks `..` and absolute paths; only relative paths allowed  
- **SSRF Protection**: Blocks `localhost`, `127.0.0.1`, private IPs by default (configurable)  
- **Container Security**:
  - `read_only=True`
  - `no-new-privileges`
  - `cap_drop=["ALL"]`
  - `network_mode="none"` (default: no network access)
  - `tmpfs=/tmp` to limit tmpfs size
- **File Filtering**: Internal files (e.g., `__sandboxd__.py`) hidden from list/download

---

## 🧰 Quick Deployment

### 1. Install Dependencies

```bash
pip install fastapi uvicorn docker boto3 botocore pydantic python-multipart python-dotenv
```

### 2. Start MinIO (Optional, if using S3)

```bash
docker run -d -p 9000:9000 -p 9001:9001 \
  -e "MINIO_ROOT_USER=minioadmin" \
  -e "MINIO_ROOT_PASSWORD=minioadmin" \
  minio/minio server /data --console-address ":9001"
```

> Web Console: `http://localhost:9001` → Create bucket `sandbox`

### 3. Start Service

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### 4. Test Health Check

```bash
curl http://localhost:8000/health
# {"status": "ok"}
```

---

## 🔄 Lifecycle Management

### Sandbox States

- **Creation**: Automatically creates container and workspace on first `/execute` call  
- **Reconnection**: After service restart, reconnects to existing containers using `container_name = sb_{user_key}_{session_key}`  
- **Reclamation**:
  - **Memory Sandboxes**: Expired sandboxes automatically removed from memory and cleaned up  
  - **Docker Orphans**: Reaper periodically scans containers with `sandbox.managed=1` label, checks `.sb_meta.json`, and cleans up if expired
- **Forced Cleanup**: Triggered via `/stop` or if `DELETE_S3_ON_CLEANUP=1`, deletes S3 data

---

## 📈 Performance & Limits

| Item                   | Description                              |
| ---------------------- | ---------------------------------------- |
| Concurrency Limit      | `SANDBOX_MAX_TOTAL` (default 50)         |
| Execution Timeout      | `SANDBOX_EXEC_TIMEOUT_SECONDS` (10s)     |
| Output Truncation      | Auto-truncated beyond `MAX_OUTPUT_BYTES` |
| Upload File Size Limit | `MAX_UPLOAD_BYTES` (default 50MB)        |
| Bulk Upload URL Limit  | Max 200 URLs                             |
| Presigned URL Expiry   | Max 7 days (604800 seconds)              |

---

## 🧪 Example: Simulate ChatGPT Conversation

```python
# First execution: Define variable
code = "x = 10\nx"

# Output: 10

# Second execution: Use previous variable
code = "x += 5\nx"

# Output: 15
```

> Variable `x` automatically persists across executions within the same `session_id`!