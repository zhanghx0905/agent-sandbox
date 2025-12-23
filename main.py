"""
Multi-tenant Code Sandbox (Docker + MinIO/S3)
ChatGPT-like: per-session persistent Python kernel (daemon) so variables persist across runs.

Key features:
- Static auth header: X-API-Key
- Multi-tenant by (user_id, session_id) in JSON body / query / form (no dynamic headers)
- Auto-create sandbox on demand (NO /init endpoint)
- Persistent variables within same session: container runs __sandboxd__.py daemon
- Reattach across API server restart: deterministic container name + labels + local meta file
- Incremental sync-out to S3 (only changed files)
- Reset kernel endpoint: clears Python globals but keeps files
- Reaper: cleans expired sessions and docker-orphans

Env vars (recommended):
- SANDBOX_API_KEY=change-me
- S3_ENDPOINT_URL=http://127.0.0.1:9000
- S3_ACCESS_KEY_ID=minioadmin
- S3_SECRET_ACCESS_KEY=minioadmin
- S3_BUCKET=sandbox
- S3_REGION=us-east-1
- S3_PREFIX_ROOT=sandboxes
- PRESIGN_EXPIRES_SECONDS=300

- SANDBOX_IMAGE=python:3.12-slim
- SANDBOX_TTL_SECONDS=1800
- SANDBOX_IDLE_SECONDS=600
- SANDBOX_EXEC_TIMEOUT_SECONDS=10
- SANDBOX_REAPER_INTERVAL=15
- SANDBOX_MAX_TOTAL=50
- WORK_ROOT=./sandboxes

- MAX_UPLOAD_BYTES=52428800
- MAX_OUTPUT_BYTES=200000

Cleanup behavior:
- DELETE_S3_ON_CLEANUP=1  (default)   -> deleting session removes S3 prefix
- DELETE_S3_ON_DELETE=0   (default)   -> if a local file is deleted, whether to delete S3 object too

Security (URL upload):
- ALLOW_PRIVATE_URLS=0 (default) -> blocks localhost/private IPs to reduce SSRF risk
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import socket
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import boto3
import docker
from botocore.config import Config
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

load_dotenv()

# -----------------------------
# Container-internal scripts
# -----------------------------

SANDBOXD_CODE = r'''# /work/__sandboxd__.py
import ast
import contextlib
import io
import json
import os
import signal
import socket
import struct
import threading
import traceback
from typing import Any, Dict, Tuple

SOCK_PATH = os.environ.get("SANDBOX_SOCK_PATH", "/tmp/sandbox.sock")
EXEC_LOCK = threading.Lock()

def _fresh_globals() -> Dict[str, Any]:
    g: Dict[str, Any] = {"__name__": "__main__", "__builtins__": __builtins__}
    g["_"] = None
    return g

globals_ns: Dict[str, Any] = _fresh_globals()

class _Timeout(Exception):
    pass

def _alarm_handler(signum, frame):
    raise _Timeout("Execution timed out")

def _exec_with_last_expr(code: str) -> Tuple[bool, str, str, str]:
    """
    ChatGPT/Jupyter-like:
    - exec in a persistent globals()
    - if last statement is expression, eval and print(repr(val)) like REPL
    """
    stdout_io = io.StringIO()
    stderr_io = io.StringIO()
    tb = ""

    try:
        mod = ast.parse(code, mode="exec")
        body = mod.body

        last_expr = None
        if body and isinstance(body[-1], ast.Expr):
            last_expr = body[-1].value
            mod.body = body[:-1]

        with contextlib.redirect_stdout(stdout_io), contextlib.redirect_stderr(stderr_io):
            if mod.body:
                exec(compile(mod, "<sandbox>", "exec"), globals_ns, globals_ns)

            if last_expr is not None:
                val = eval(compile(ast.Expression(last_expr), "<sandbox>", "eval"), globals_ns, globals_ns)
                globals_ns["_"] = val
                if val is not None:
                    print(repr(val))

        return True, stdout_io.getvalue(), stderr_io.getvalue(), ""
    except Exception:
        tb = traceback.format_exc()
        return False, stdout_io.getvalue(), stderr_io.getvalue(), tb

def _handle(req: Dict[str, Any]) -> Dict[str, Any]:
    op = req.get("op") or "exec"

    if op == "ping":
        return {"ok": True, "stdout": "", "stderr": "", "traceback": ""}

    if op == "reset":
        global globals_ns
        globals_ns = _fresh_globals()
        return {"ok": True, "stdout": "Kernel reset.\n", "stderr": "", "traceback": ""}

    # exec
    code = req.get("code", "")
    timeout_s = int(req.get("timeout_s", 10))
    if not isinstance(code, str) or not code.strip():
        return {"ok": False, "stdout": "", "stderr": "", "traceback": "Empty code"}

    with EXEC_LOCK:
        old = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, _alarm_handler)
        signal.setitimer(signal.ITIMER_REAL, max(1, timeout_s))
        try:
            ok, out, err, tb = _exec_with_last_expr(code)
            return {"ok": ok, "stdout": out, "stderr": err, "traceback": tb}
        except _Timeout:
            return {"ok": False, "stdout": "", "stderr": "", "traceback": "TimeoutError: execution timed out"}
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)

def _recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf

def serve() -> None:
    try:
        os.unlink(SOCK_PATH)
    except FileNotFoundError:
        pass

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_PATH)
    srv.listen(16)

    while True:
        conn, _ = srv.accept()
        try:
            hdr = _recv_exact(conn, 4)
            (length,) = struct.unpack(">I", hdr)
            payload = _recv_exact(conn, length)
            req = json.loads(payload.decode("utf-8", "replace"))
            resp = _handle(req)
            data = json.dumps(resp, ensure_ascii=False).encode("utf-8")
            conn.sendall(struct.pack(">I", len(data)) + data)
        except Exception as e:
            err = json.dumps(
                {"ok": False, "stdout": "", "stderr": "", "traceback": repr(e)},
                ensure_ascii=False
            ).encode("utf-8")
            conn.sendall(struct.pack(">I", len(err)) + err)
        finally:
            try:
                conn.close()
            except Exception:
                pass

if __name__ == "__main__":
    serve()
'''

SB_CLIENT_CODE = r"""# /work/__sb_client__.py
import argparse
import json
import socket
import struct

def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sock", default="/tmp/sandbox.sock")
    p.add_argument("--code-file", required=False)
    p.add_argument("--timeout-s", type=int, default=10)
    p.add_argument("--op", default="exec", choices=["exec", "ping", "reset"])
    args = p.parse_args()

    code = ""
    if args.op == "exec":
        if not args.code_file:
            raise SystemExit("--code-file is required for exec")
        with open(args.code_file, "r", encoding="utf-8") as f:
            code = f.read()

    req = {"op": args.op, "code": code, "timeout_s": args.timeout_s}
    data = json.dumps(req, ensure_ascii=False).encode("utf-8")

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(args.sock)
    s.sendall(struct.pack(">I", len(data)) + data)

    hdr = recv_exact(s, 4)
    (length,) = struct.unpack(">I", hdr)
    payload = recv_exact(s, length)
    s.close()

    print(payload.decode("utf-8", "replace"))

if __name__ == "__main__":
    main()
"""

# -----------------------------
# Helpers: auth + safe paths
# -----------------------------


def require_api_key(req: Request) -> None:
    expected = os.environ.get("SANDBOX_API_KEY", "change-me")
    got = req.headers.get("x-api-key")
    if not got or got != expected:
        raise HTTPException(status_code=401, detail="Invalid API key")


def _sha16(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def safe_rel_path(user_path: str) -> str:
    if not user_path or user_path.strip() == "":
        raise ValueError("path is required")

    p = user_path.replace("\\", "/").strip()
    if p.startswith("/"):
        raise ValueError("absolute paths are not allowed")
    if ".." in p.split("/"):
        raise ValueError(".. is not allowed")

    parts = [x for x in p.split("/") if x not in ("", ".")]
    if not parts:
        raise ValueError("invalid path")
    return "/".join(parts)


INTERNAL_FILES = {
    "__mcp_exec__.py",
    "__sandboxd__.py",
    "__sb_client__.py",
    ".sb_meta.json",
    ".sb_snapshot.json",
}


def list_files(root: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not root.exists():
        return out
    for p in root.rglob("*"):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            if rel in INTERNAL_FILES:
                continue
            try:
                st = p.stat()
                out.append(
                    {"path": rel, "size_bytes": st.st_size, "mtime": int(st.st_mtime)}
                )
            except OSError:
                pass
    out.sort(key=lambda x: x["path"])
    return out


# -----------------------------
# Identity: derived from params
# -----------------------------


@dataclass(frozen=True)
class Identity:
    user_key: str
    session_key: str


def resolve_identity_from_params(user_id: str, session_id: Optional[str]) -> Identity:
    if not user_id or not user_id.strip():
        raise HTTPException(status_code=400, detail="user_id required")
    user_key = _sha16(f"user:{user_id.strip()}")
    sess = (session_id or "default").strip()
    session_key = _sha16(f"sess:{sess}")
    return Identity(user_key=user_key, session_key=session_key)


# -----------------------------
# S3/MinIO Store (lazy bucket ensure)
# -----------------------------


class S3Store:
    def __init__(self) -> None:
        self.endpoint_url = os.environ.get("S3_ENDPOINT_URL", "").strip()
        self.access_key = os.environ.get("S3_ACCESS_KEY_ID", "").strip()
        self.secret_key = os.environ.get("S3_SECRET_ACCESS_KEY", "").strip()
        self.bucket = os.environ.get("S3_BUCKET", "").strip()
        self.region = os.environ.get("S3_REGION", "us-east-1").strip()
        self.prefix_root = os.environ.get("S3_PREFIX_ROOT", "sandboxes").strip()
        self.presign_default = int(os.environ.get("PRESIGN_EXPIRES_SECONDS", "300"))

        if (
            not self.endpoint_url
            or not self.access_key
            or not self.secret_key
            or not self.bucket
        ):
            raise RuntimeError(
                "Missing S3 env vars: S3_ENDPOINT_URL/S3_ACCESS_KEY_ID/S3_SECRET_ACCESS_KEY/S3_BUCKET"
            )

        self.client = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=self.region,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

        self._bucket_checked = False
        self._bucket_lock = threading.Lock()

    def ensure_bucket(self) -> None:
        if self._bucket_checked:
            return
        with self._bucket_lock:
            if self._bucket_checked:
                return
            try:
                self.client.head_bucket(Bucket=self.bucket)
            except ClientError:
                self.client.create_bucket(Bucket=self.bucket)
            self._bucket_checked = True

    @staticmethod
    def clamp_expires(expires_in: Optional[int], default: int) -> int:
        exp = int(expires_in or default)
        if exp < 1:
            exp = 1
        if exp > 604800:
            exp = 604800
        return exp

    def prefix_for(self, ident: Identity) -> str:
        return f"{self.prefix_root}/{ident.user_key}/{ident.session_key}/"

    def key_for(self, ident: Identity, rel_path: str) -> str:
        rel = safe_rel_path(rel_path)
        return f"{self.prefix_root}/{ident.user_key}/{ident.session_key}/{rel}"

    def presign_get(self, key: str, expires_in: Optional[int] = None) -> str:
        self.ensure_bucket()
        exp = self.clamp_expires(expires_in, self.presign_default)
        return self.client.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=exp,
        )

    def object_exists(self, key: str) -> bool:
        self.ensure_bucket()
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def download_to_path(self, key: str, local_path: Path) -> None:
        self.ensure_bucket()
        local_path.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, key, str(local_path))

    def upload_from_path(self, local_path: Path, key: str) -> None:
        self.ensure_bucket()
        extra: Dict[str, Any] = {}
        mime, _ = mimetypes.guess_type(local_path.name)
        if mime:
            extra["ContentType"] = mime
        # boto3 doesn't like ExtraArgs=None in some versions; pass only if present.
        if extra:
            self.client.upload_file(str(local_path), self.bucket, key, ExtraArgs=extra)
        else:
            self.client.upload_file(str(local_path), self.bucket, key)

    def delete_key(self, key: str) -> None:
        self.ensure_bucket()
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception:
            pass

    def delete_prefix(self, prefix: str) -> None:
        self.ensure_bucket()
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            contents = page.get("Contents", [])
            if not contents:
                continue
            self.client.delete_objects(
                Bucket=self.bucket,
                Delete={
                    "Objects": [{"Key": obj["Key"]} for obj in contents],
                    "Quiet": True,
                },
            )


# -----------------------------
# Docker Sandbox (per tenant, persistent kernel)
# -----------------------------


class DockerSandbox:
    def __init__(
        self,
        store: S3Store,
        ident: Identity,
        work_root: str,
        ttl_seconds: int,
        idle_seconds: int,
        exec_timeout_seconds: int,
        image: str,
        mem_limit: str = "512m",
        cpu_quota: int = 50000,
        pids_limit: int = 100,
        delete_s3_on_cleanup: bool = True,
    ) -> None:
        self.store = store
        self.ident = ident
        self.work_root = Path(work_root).resolve()
        self.work_root.mkdir(parents=True, exist_ok=True)

        self.ttl_seconds = ttl_seconds
        self.idle_seconds = idle_seconds
        self.exec_timeout_seconds = exec_timeout_seconds
        self.image = image

        self.mem_limit = mem_limit
        self.cpu_quota = cpu_quota
        self.pids_limit = pids_limit
        self.delete_s3_on_cleanup = delete_s3_on_cleanup

        self.docker_client = docker.from_env()
        self.container = None
        self.container_id: Optional[str] = None
        self.workdir_host: Optional[Path] = None

        self.created_at: Optional[float] = None
        self.last_used_at: Optional[float] = None

        self._lock = threading.RLock()

    # ---------- naming / metadata ----------
    @property
    def container_name(self) -> str:
        return f"sb_{self.ident.user_key}_{self.ident.session_key}"

    @property
    def meta_path(self) -> Path:
        assert self.workdir_host is not None
        return self.workdir_host / ".sb_meta.json"

    @property
    def snapshot_path(self) -> Path:
        assert self.workdir_host is not None
        return self.workdir_host / ".sb_snapshot.json"

    def _load_meta_if_any(self) -> None:
        if not self.workdir_host:
            return
        p = self.workdir_host / ".sb_meta.json"
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            self.created_at = float(data.get("created_at") or 0) or self.created_at
            self.last_used_at = (
                float(data.get("last_used_at") or 0) or self.last_used_at
            )
            self.container_id = data.get("container_id") or self.container_id
            self.image = data.get("image") or self.image
        except Exception:
            pass

    def _write_meta(self) -> None:
        if not self.workdir_host:
            return
        data = {
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "container_id": self.container_id,
            "image": self.image,
            "user_key": self.ident.user_key,
            "session_key": self.ident.session_key,
        }
        try:
            (self.workdir_host / ".sb_meta.json").write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    def _touch(self) -> None:
        self.last_used_at = time.time()
        self._write_meta()

    def expired(self) -> bool:
        # lazy load meta if timestamps missing
        if self.workdir_host and (self.created_at is None or self.last_used_at is None):
            self._load_meta_if_any()

        if not self.created_at or not self.last_used_at:
            return True
        now = time.time()
        return (now - self.created_at > self.ttl_seconds) or (
            now - self.last_used_at > self.idle_seconds
        )

    # ---------- scripts ----------
    def _ensure_scripts(self) -> None:
        assert self.workdir_host is not None
        (self.workdir_host / "__sandboxd__.py").write_text(
            SANDBOXD_CODE, encoding="utf-8"
        )
        (self.workdir_host / "__sb_client__.py").write_text(
            SB_CLIENT_CODE, encoding="utf-8"
        )

    # ---------- container lifecycle ----------
    def _start_new_container(self) -> None:
        assert self.workdir_host is not None
        labels = {
            "sandbox.managed": "1",
            "sandbox.user_key": self.ident.user_key,
            "sandbox.session_key": self.ident.session_key,
        }

        # NOTE: read_only=True but /work bind is rw, /tmp tmpfs is rw
        c = self.docker_client.containers.run(
            self.image,
            name=self.container_name,
            command=["python", "-u", "/work/__sandboxd__.py"],
            detach=True,
            tty=True,
            mem_limit=self.mem_limit,
            cpu_quota=self.cpu_quota,
            pids_limit=self.pids_limit,
            network_mode="none",
            read_only=True,
            tmpfs={"/tmp": "rw,size=64m,noexec,nodev,nosuid"},
            security_opt=["no-new-privileges:true"],
            cap_drop=["ALL"],
            labels=labels,
            volumes={str(self.workdir_host): {"bind": "/work", "mode": "rw"}},
            working_dir="/work",
            environment={"SANDBOX_SOCK_PATH": "/tmp/sandbox.sock"},
        )
        self.container = c
        self.container_id = c.id
        self.created_at = time.time()
        self.last_used_at = self.created_at
        self._write_meta()

    def attach_existing(self, container) -> None:
        # attach to an existing container & local workdir
        self.container = container
        self.container_id = container.id

        host_dir = (
            self.work_root / self.ident.user_key / self.ident.session_key
        ).resolve()
        host_dir.mkdir(parents=True, exist_ok=True)
        self.workdir_host = host_dir

        self._ensure_scripts()
        self._load_meta_if_any()

        # if meta missing, treat now as created
        if not self.created_at:
            self.created_at = time.time()
        if not self.last_used_at:
            self.last_used_at = time.time()
        self._write_meta()

    def create_or_attach(self) -> Dict[str, Any]:
        with self._lock:
            # ensure host workdir
            host_dir = (
                self.work_root / self.ident.user_key / self.ident.session_key
            ).resolve()
            host_dir.mkdir(parents=True, exist_ok=True)
            self.workdir_host = host_dir
            try:
                os.chmod(host_dir, 0o777)
            except Exception:
                pass

            self._ensure_scripts()
            self._load_meta_if_any()

            # Try attach by deterministic name
            try:
                c = self.docker_client.containers.get(self.container_name)
                self.attach_existing(c)
                # If expired, cleanup and recreate
                if self.expired():
                    self.cleanup()
                    self._start_new_container()
            except docker.errors.NotFound:
                # fresh create
                self._start_new_container()
            except Exception:
                # fallback: recreate if attach fails
                self.cleanup()
                self._start_new_container()

            self._touch()
            return {"container_id": self.container_id}

    def ensure_alive(self) -> Tuple[bool, Optional[str]]:
        with self._lock:
            if not self.container:
                return False, "Sandbox not available"
            if self.expired():
                self.cleanup()
                return False, "Sandbox expired (TTL/idle timeout)."
            try:
                self.container.reload()
                if self.container.status != "running":
                    self.container.start()
            except Exception:
                return False, "Sandbox container unavailable"
            return True, None

    # ---------- daemon calls ----------
    def _exec_client(
        self, op: str, code_file: Optional[str] = None, timeout_s: int = 10
    ) -> Tuple[int, str]:
        """
        Run client inside container. Returns (exit_code, stdout_text).
        Client prints one-line JSON from daemon if ok.
        """
        assert self.container is not None
        cmd = [
            "python",
            "/work/__sb_client__.py",
            "--sock",
            "/tmp/sandbox.sock",
            "--op",
            op,
            "--timeout-s",
            str(timeout_s),
        ]
        if op == "exec":
            assert code_file is not None
            cmd += ["--code-file", code_file]
        r = self.container.exec_run(cmd=cmd, workdir="/work")
        text = (r.output or b"").decode("utf-8", "replace").strip()
        return int(r.exit_code or 0), text

    def _ensure_daemon_ready(self) -> bool:
        ok, _ = self.ensure_alive()
        if not ok:
            return False
        assert self.container is not None
        try:
            _, raw = self._exec_client(op="ping", timeout_s=3)
            resp = json.loads(raw)
            return bool(resp.get("ok"))
        except Exception:
            # daemon likely not ready; try restart container
            try:
                self.container.restart(timeout=3)
            except Exception:
                return False
            try:
                _, raw2 = self._exec_client(op="ping", timeout_s=3)
                resp2 = json.loads(raw2)
                return bool(resp2.get("ok"))
            except Exception:
                return False

    def reset_kernel(self) -> Dict[str, Any]:
        with self._lock:
            if not self._ensure_daemon_ready():
                return {"error": "Kernel not ready"}
            try:
                _, raw = self._exec_client(op="reset", timeout_s=5)
                resp = json.loads(raw)
            except Exception:
                return {"error": "Reset failed"}
            self._touch()
            return {
                "ok": bool(resp.get("ok")),
                "output": (resp.get("stdout") or "") + (resp.get("traceback") or ""),
            }

    # ---------- S3 sync ----------
    def sync_in(self, paths: List[str]) -> Dict[str, Any]:
        ok, err = self.ensure_alive()
        if not ok:
            return {"error": err}
        assert self.workdir_host is not None

        pulled: List[str] = []
        missing: List[str] = []

        with self._lock:
            for p in paths:
                rel = safe_rel_path(p)
                key = self.store.key_for(self.ident, rel)
                if not self.store.object_exists(key):
                    missing.append(rel)
                    continue
                self.store.download_to_path(key, self.workdir_host / rel)
                pulled.append(rel)
            self._touch()

        return {"pulled": pulled, "missing": missing}

    def _load_snapshot(self) -> Dict[str, Dict[str, Any]]:
        assert self.workdir_host is not None
        p = self.snapshot_path
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def _save_snapshot(self, snap: Dict[str, Dict[str, Any]]) -> None:
        assert self.workdir_host is not None
        try:
            self.snapshot_path.write_text(
                json.dumps(snap, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    def sync_out_changed(self) -> Dict[str, Any]:
        ok, err = self.ensure_alive()
        if not ok:
            return {"error": err}
        assert self.workdir_host is not None

        files = list_files(self.workdir_host)  # excludes internal files
        prev = self._load_snapshot()
        curr: Dict[str, Dict[str, Any]] = {}

        changed: List[str] = []
        uploaded: List[str] = []
        deleted: List[str] = []

        for f in files:
            rel = f["path"]
            curr[rel] = {"size_bytes": f["size_bytes"], "mtime": f["mtime"]}
            old = prev.get(rel)
            if (
                (old is None)
                or (old.get("size_bytes") != f["size_bytes"])
                or (old.get("mtime") != f["mtime"])
            ):
                changed.append(rel)

        for rel in prev.keys():
            if rel not in curr:
                deleted.append(rel)

        delete_on_delete = os.environ.get("DELETE_S3_ON_DELETE", "0") == "1"

        with self._lock:
            for rel in changed:
                key = self.store.key_for(self.ident, rel)
                self.store.upload_from_path(self.workdir_host / rel, key)
                uploaded.append(rel)

            if delete_on_delete:
                for rel in deleted:
                    key = self.store.key_for(self.ident, rel)
                    self.store.delete_key(key)

            self._save_snapshot(curr)
            self._touch()

        return {"uploaded": uploaded, "deleted": deleted, "files": files}

    # ---------- execution ----------
    def run_code(self, code: str, inputs: Optional[List[str]] = None) -> Dict[str, Any]:
        ok, err = self.ensure_alive()
        if not ok:
            return {"error": err}
        assert self.workdir_host is not None

        if inputs:
            r = self.sync_in(inputs)
            if "error" in r:
                return r

        # ensure daemon
        with self._lock:
            if not self._ensure_daemon_ready():
                return {"error": "Kernel not ready"}

            exec_file = self.workdir_host / "__mcp_exec__.py"
            exec_file.write_text(code, encoding="utf-8")

            timeout_s = max(1, int(self.exec_timeout_seconds))
            max_out = int(os.environ.get("MAX_OUTPUT_BYTES", "200000"))

            # Try once; if bad response, restart container and retry once
            def _do_exec() -> Dict[str, Any]:
                _, raw = self._exec_client(
                    op="exec", code_file="/work/__mcp_exec__.py", timeout_s=timeout_s
                )
                resp = json.loads(raw)
                stdout = resp.get("stdout") or ""
                stderr = resp.get("stderr") or ""
                tb = resp.get("traceback") or ""
                okk = bool(resp.get("ok"))

                output = stdout + stderr
                if tb:
                    output += (
                        "\n" if output and not output.endswith("\n") else ""
                    ) + tb
                    if not output.endswith("\n"):
                        output += "\n"

                if len(output.encode("utf-8", "ignore")) > max_out:
                    # truncate by characters (approx)
                    output = output[: max(0, max_out // 2)] + "\n...[truncated]...\n"

                return {"ok": okk, "output": output}

            try:
                exec_resp = _do_exec()
            except Exception:
                try:
                    assert self.container is not None
                    self.container.restart(timeout=3)
                    exec_resp = _do_exec()
                except Exception:
                    return {"error": "Execution failed (kernel unavailable)"}

            self._touch()

        out = self.sync_out_changed()
        if "error" in out:
            return {
                "exit_code": 1,
                "output": exec_resp.get("output", ""),
                "sync_error": out["error"],
            }

        exit_code = 0 if exec_resp.get("ok") else 1
        return {
            "exit_code": exit_code,
            "output": exec_resp.get("output"),
            "files": out["files"],
        }

    # ---------- cleanup ----------
    def cleanup(self) -> None:
        with self._lock:
            # stop/remove container
            if self.container:
                try:
                    try:
                        self.container.stop(timeout=3)
                    except Exception:
                        pass
                    try:
                        self.container.remove(force=True)
                    except Exception:
                        pass
                finally:
                    self.container = None
                    self.container_id = None

            # remove local dir
            if self.workdir_host and self.workdir_host.exists():
                shutil.rmtree(self.workdir_host, ignore_errors=True)
            self.workdir_host = None

            # remove s3 prefix
            if self.delete_s3_on_cleanup:
                try:
                    self.store.delete_prefix(self.store.prefix_for(self.ident))
                except Exception:
                    pass

            self.created_at = None
            self.last_used_at = None


# -----------------------------
# Sandbox Manager (multi-tenant, reattach, reaper)
# -----------------------------


class SandboxManager:
    def __init__(
        self,
        store: S3Store,
        work_root: str,
        ttl_seconds: int,
        idle_seconds: int,
        exec_timeout_seconds: int,
        default_image: str,
        max_total: int,
        delete_s3_on_cleanup: bool = True,
    ) -> None:
        self.store = store
        self.work_root = work_root
        self.ttl_seconds = ttl_seconds
        self.idle_seconds = idle_seconds
        self.exec_timeout_seconds = exec_timeout_seconds
        self.default_image = default_image
        self.max_total = max_total
        self.delete_s3_on_cleanup = delete_s3_on_cleanup

        self._lock = threading.RLock()
        self._sandboxes: Dict[Tuple[str, str], DockerSandbox] = {}

        self._stop_event = asyncio.Event()
        self._reaper_task: Optional[asyncio.Task] = None

        self._docker_client = docker.from_env()

    def _new_sb(self, ident: Identity, image: Optional[str]) -> DockerSandbox:
        return DockerSandbox(
            store=self.store,
            ident=ident,
            work_root=self.work_root,
            ttl_seconds=self.ttl_seconds,
            idle_seconds=self.idle_seconds,
            exec_timeout_seconds=self.exec_timeout_seconds,
            image=image or self.default_image,
            delete_s3_on_cleanup=self.delete_s3_on_cleanup,
        )

    def get_or_create(
        self, ident: Identity, image: Optional[str] = None
    ) -> DockerSandbox:
        key = (ident.user_key, ident.session_key)

        with self._lock:
            sb = self._sandboxes.get(key)
            if sb is None:
                sb = self._new_sb(ident, image)
                sb.create_or_attach()
                self._sandboxes[key] = sb
            else:
                # update image only if provided explicitly
                if image:
                    sb.image = image
                # ensure alive; if expired recreate
                if sb.expired():
                    sb.cleanup()
                    sb = self._new_sb(ident, image)
                    sb.create_or_attach()
                    self._sandboxes[key] = sb
                else:
                    sb.create_or_attach()  # attach if container got recreated outside

            # enforce global cap
            if len(self._sandboxes) > self.max_total:
                victims = sorted(
                    self._sandboxes.items(), key=lambda kv: (kv[1].last_used_at or 0)
                )
                while len(self._sandboxes) > self.max_total and victims:
                    (k, v) = victims.pop(0)
                    try:
                        v.cleanup()
                    finally:
                        self._sandboxes.pop(k, None)

            return sb

    def stop(self, ident: Identity) -> None:
        key = (ident.user_key, ident.session_key)
        with self._lock:
            sb = self._sandboxes.pop(key, None)
        if sb:
            sb.cleanup()
        else:
            # try remove by name (in case not in memory)
            name = f"sb_{ident.user_key}_{ident.session_key}"
            try:
                c = self._docker_client.containers.get(name)
                try:
                    c.stop(timeout=3)
                except Exception:
                    pass
                try:
                    c.remove(force=True)
                except Exception:
                    pass
            except Exception:
                pass
            # local dir + s3 cleanup
            host_dir = (
                Path(self.work_root).resolve() / ident.user_key / ident.session_key
            )
            if host_dir.exists():
                shutil.rmtree(host_dir, ignore_errors=True)
            if self.delete_s3_on_cleanup:
                try:
                    self.store.delete_prefix(self.store.prefix_for(ident))
                except Exception:
                    pass

    def _reap_in_memory(self) -> None:
        with self._lock:
            items = list(self._sandboxes.items())
        for k, sb in items:
            if sb.expired():
                try:
                    sb.cleanup()
                finally:
                    with self._lock:
                        self._sandboxes.pop(k, None)

    def _reap_docker_orphans(self) -> None:
        """
        Clean containers that match our labels but are not in memory,
        using local meta timestamps to decide expiration.
        """
        try:
            containers = self._docker_client.containers.list(
                all=True, filters={"label": "sandbox.managed=1"}
            )
        except Exception:
            return

        for c in containers:
            try:
                labels = c.labels or {}
                uk = labels.get("sandbox.user_key")
                sk = labels.get("sandbox.session_key")
                if not uk or not sk:
                    continue

                key = (uk, sk)
                with self._lock:
                    if key in self._sandboxes:
                        continue

                # read meta from local disk to decide expiry
                host_dir = Path(self.work_root).resolve() / uk / sk
                meta = host_dir / ".sb_meta.json"
                expired = True
                if meta.exists():
                    try:
                        data = json.loads(meta.read_text(encoding="utf-8"))
                        created_at = float(data.get("created_at") or 0)
                        last_used_at = float(data.get("last_used_at") or 0)
                        if created_at and last_used_at:
                            now = time.time()
                            expired = (now - created_at > self.ttl_seconds) or (
                                now - last_used_at > self.idle_seconds
                            )
                    except Exception:
                        expired = True

                if expired:
                    try:
                        try:
                            c.stop(timeout=3)
                        except Exception:
                            pass
                        try:
                            c.remove(force=True)
                        except Exception:
                            pass
                    finally:
                        if host_dir.exists():
                            shutil.rmtree(host_dir, ignore_errors=True)

                        if self.delete_s3_on_cleanup:
                            try:
                                ident = Identity(user_key=uk, session_key=sk)
                                self.store.delete_prefix(self.store.prefix_for(ident))
                            except Exception:
                                pass
            except Exception:
                pass

    def reap_once(self) -> None:
        self._reap_in_memory()
        self._reap_docker_orphans()

    async def start_reaper(self, interval: int = 15) -> None:
        if self._reaper_task:
            return

        async def _loop() -> None:
            while not self._stop_event.is_set():
                await asyncio.sleep(interval)
                await asyncio.to_thread(self.reap_once)

        self._reaper_task = asyncio.create_task(_loop())

    async def shutdown(self) -> None:
        self._stop_event.set()
        if self._reaper_task:
            self._reaper_task.cancel()

        with self._lock:
            items = list(self._sandboxes.values())
            self._sandboxes.clear()

        for sb in items:
            try:
                sb.cleanup()
            except Exception:
                pass


# -----------------------------
# URL batch upload helpers (SSRF-safe by default)
# -----------------------------

_FILENAME_RE = re.compile(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', re.IGNORECASE)


def _extract_filename_from_content_disposition(cd_value: str) -> Optional[str]:
    if not cd_value:
        return None
    m = _FILENAME_RE.search(cd_value)
    if not m:
        return None
    name = m.group(1).strip()
    try:
        name = urllib.parse.unquote(name)
    except Exception:
        pass
    return name or None


def _filename_from_url(url: str) -> str:
    """
    Prefer query param Content-Disposition=...filename="xxx"
    else fallback to URL path basename.
    """
    u = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(u.query)

    cd_list = q.get("Content-Disposition") or q.get("content-disposition")
    if cd_list:
        fn = _extract_filename_from_content_disposition(cd_list[0])
        if fn:
            return fn

    basename = (u.path.split("/")[-1] or "").strip()
    if basename:
        return basename

    return "file.bin"


def _download_url_to_path(
    url: str, out_path: Path, max_bytes: int, timeout_s: int = 20
) -> int:
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=400, detail=f"unsupported url scheme: {u.scheme}"
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)

    req = urllib.request.Request(
        url,
        method="GET",
    )

    read = 0
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        with open(out_path, "wb") as f:
            while True:
                chunk = resp.read(1024 * 1024)  # 1MB
                if not chunk:
                    break
                read += len(chunk)
                if read > max_bytes:
                    raise HTTPException(
                        status_code=413, detail=f"file too large > {max_bytes} bytes"
                    )
                f.write(chunk)

    return read


# -----------------------------
# FastAPI Models
# -----------------------------


class ExecuteBody(BaseModel):
    user_id: str
    session_id: Optional[str] = None
    code: str
    inputs: Optional[List[str]] = None
    presign_expires_in: Optional[int] = None


class ResetBody(BaseModel):
    user_id: str
    session_id: Optional[str] = None


class StopBody(BaseModel):
    user_id: str
    session_id: Optional[str] = None


class UploadUrlsBody(BaseModel):
    user_id: str
    session_id: Optional[str] = None
    urls: List[str] = list()
    presign_expires_in: Optional[int] = None
    dest_dir: Optional[str] = None  # optional directory inside /work


# -----------------------------
# App + Lifespan
# -----------------------------

app = FastAPI(title="FastGPT-friendly Sandbox (Docker + MinIO/S3) - ChatGPT-like")

STORE: Optional[S3Store] = None
MANAGER: Optional[SandboxManager] = None


@app.on_event("startup")
async def _startup() -> None:
    global STORE, MANAGER

    STORE = S3Store()
    WORK_ROOT = os.environ.get("WORK_ROOT", "./sandboxes")
    ttl = int(os.environ.get("SANDBOX_TTL_SECONDS", "1800"))
    idle = int(os.environ.get("SANDBOX_IDLE_SECONDS", "600"))
    exec_to = int(os.environ.get("SANDBOX_EXEC_TIMEOUT_SECONDS", "10"))
    reaper_interval = int(os.environ.get("SANDBOX_REAPER_INTERVAL", "15"))
    image = os.environ.get("SANDBOX_IMAGE", "python:3.12-slim")
    max_total = int(os.environ.get("SANDBOX_MAX_TOTAL", "50"))

    delete_s3 = os.environ.get("DELETE_S3_ON_CLEANUP", "1") != "0"

    MANAGER = SandboxManager(
        store=STORE,
        work_root=WORK_ROOT,
        ttl_seconds=ttl,
        idle_seconds=idle,
        exec_timeout_seconds=exec_to,
        default_image=image,
        max_total=max_total,
        delete_s3_on_cleanup=delete_s3,
    )
    await MANAGER.start_reaper(interval=reaper_interval)


@app.on_event("shutdown")
async def _shutdown() -> None:
    global STORE, MANAGER
    if MANAGER:
        await MANAGER.shutdown()
    MANAGER = None
    STORE = None


def store_or_die() -> S3Store:
    if STORE is None:
        raise HTTPException(status_code=500, detail="Store not initialized")
    return STORE


def manager_or_die() -> SandboxManager:
    if MANAGER is None:
        raise HTTPException(status_code=500, detail="Manager not initialized")
    return MANAGER


# -----------------------------
# Routes
# -----------------------------


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok"}


@app.post("/v1/execute")
async def v1_execute(req: Request, body: ExecuteBody):
    require_api_key(req)
    ident = resolve_identity_from_params(body.user_id, body.session_id)

    sb = await asyncio.to_thread(manager_or_die().get_or_create, ident, None)
    r = await asyncio.to_thread(sb.run_code, body.code, body.inputs)

    if "error" in r:
        return {"error": r["error"]}

    st = store_or_die()
    expires = body.presign_expires_in

    artifacts: List[Dict[str, Any]] = []
    for f in r.get("files", []):
        rel = f["path"]
        key = st.key_for(ident, rel)
        download_url = st.presign_get(key=key, expires_in=expires)
        mime, _ = mimetypes.guess_type(rel)
        artifacts.append(
            {
                "path": rel,
                "size_bytes": f["size_bytes"],
                "mime_type": mime or "application/octet-stream",
                "download_url": download_url,
            }
        )

    return {"output": r.get("output"), "artifacts": artifacts}


@app.post("/v1/sandbox/reset")
async def v1_reset(req: Request, body: ResetBody) -> Dict[str, Any]:
    """
    Reset kernel variables (like ChatGPT/Jupyter: restart kernel) but keep files in /work.
    """
    require_api_key(req)
    ident = resolve_identity_from_params(body.user_id, body.session_id)

    sb = await asyncio.to_thread(manager_or_die().get_or_create, ident, None)
    r = await asyncio.to_thread(sb.reset_kernel)

    if "error" in r:
        return {"status": "error", "message": r["error"]}

    return {
        "status": "success",
        "output": r.get("output", ""),
        "container_id": sb.container_id,
    }


@app.post("/v1/sandbox/stop")
async def v1_stop(req: Request, body: StopBody) -> Dict[str, Any]:
    """
    Fully stop & cleanup one sandbox session (container + local dir + optional S3 prefix).
    """
    require_api_key(req)
    ident = resolve_identity_from_params(body.user_id, body.session_id)
    await asyncio.to_thread(manager_or_die().stop, ident)
    return {"status": "success"}


# ---- OLD multipart single-file upload (kept for backward compatibility) ----
@app.post("/v1/files/upload")
async def v1_upload(
    req: Request,
    user_id: str = Form(...),
    session_id: Optional[str] = Form(None),
    path: str = Form(...),
    file: UploadFile = File(...),
    presign_expires_in: Optional[int] = Form(None),
) -> Dict[str, Any]:
    require_api_key(req)
    ident = resolve_identity_from_params(user_id, session_id)

    sb = await asyncio.to_thread(manager_or_die().get_or_create, ident, None)

    rel = safe_rel_path(path)
    if rel in INTERNAL_FILES:
        raise HTTPException(status_code=400, detail="forbidden filename")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")

    max_bytes = int(os.environ.get("MAX_UPLOAD_BYTES", "52428800"))
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=413, detail=f"file too large > {max_bytes} bytes"
        )

    assert sb.workdir_host is not None
    host_path = (sb.workdir_host / rel).resolve()

    if sb.workdir_host not in host_path.parents and host_path != sb.workdir_host:
        raise HTTPException(status_code=400, detail="invalid path (escape)")

    host_path.parent.mkdir(parents=True, exist_ok=True)
    host_path.write_bytes(data)

    st = store_or_die()
    key = st.key_for(ident, rel)
    await asyncio.to_thread(st.upload_from_path, host_path, key)

    download_url = st.presign_get(key=key, expires_in=presign_expires_in)
    mime, _ = mimetypes.guess_type(rel)

    return {
        "status": "success",
        "path": rel,
        "size_bytes": len(data),
        "mime_type": mime or "application/octet-stream",
        "download_url": download_url,
        "container_id": sb.container_id,
    }


# ---- NEW: batch upload by URL list (your requested behavior) ----
@app.post("/v1/files/upload-urls")
async def v1_upload_urls(req: Request, body: UploadUrlsBody) -> Dict[str, Any]:
    require_api_key(req)
    ident = resolve_identity_from_params(body.user_id, body.session_id)

    # if not body.urls:
    #     raise HTTPException(status_code=400, detail="urls is required")
    if len(body.urls) > 200:
        raise HTTPException(status_code=400, detail="too many urls (max 200)")

    sb = await asyncio.to_thread(manager_or_die().get_or_create, ident, None)

    max_bytes = int(os.environ.get("MAX_UPLOAD_BYTES", "52428800"))
    st = store_or_die()

    dest_dir = (body.dest_dir or "").strip()
    if dest_dir:
        dest_dir = dest_dir.replace("\\", "/").strip().strip("/")
        if ".." in dest_dir.split("/"):
            raise HTTPException(status_code=400, detail="invalid dest_dir")

    assert sb.workdir_host is not None

    uploaded: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for url in body.urls:
        try:
            filename = _filename_from_url(url)

            rel = filename
            if dest_dir:
                rel = f"{dest_dir}/{filename}"

            rel = safe_rel_path(rel)
            if rel in INTERNAL_FILES:
                raise HTTPException(
                    status_code=400, detail=f"forbidden filename: {rel}"
                )

            host_path = (sb.workdir_host / rel).resolve()
            if (
                sb.workdir_host not in host_path.parents
                and host_path != sb.workdir_host
            ):
                raise HTTPException(status_code=400, detail="invalid path (escape)")

            nbytes = await asyncio.to_thread(
                _download_url_to_path, url, host_path, max_bytes, 20
            )

            key = st.key_for(ident, rel)
            await asyncio.to_thread(st.upload_from_path, host_path, key)

            # download_url = st.presign_get(key=key, expires_in=body.presign_expires_in)
            # mime, _ = mimetypes.guess_type(rel)

            uploaded.append(
                rel
                # {
                #     "path": rel,
                #     # "size_bytes": nbytes,
                #     # "mime_type": mime or "application/octet-stream",
                #     # "download_url": download_url,
                #     # "source_url": url,
                # }
            )
        except Exception as e:
            errors.append({"source_url": url, "error": str(e)})

    return {
        "status": (
            "success" if not errors else ("partial_success" if uploaded else "error")
        ),
        "uploaded": uploaded,
        "errors": errors,
    }


@app.get("/v1/files/list")
async def v1_list_files(
    req: Request,
    user_id: str = Query(...),
    session_id: Optional[str] = Query(None),
) -> Dict[str, Any]:
    require_api_key(req)
    ident = resolve_identity_from_params(user_id, session_id)

    sb = await asyncio.to_thread(manager_or_die().get_or_create, ident, None)
    ok, err = sb.ensure_alive()
    if not ok:
        raise HTTPException(status_code=400, detail=err)

    assert sb.workdir_host is not None
    return {"files": list_files(sb.workdir_host)}


@app.get("/v1/files/download-url")
async def v1_download_url(
    req: Request,
    user_id: str = Query(...),
    session_id: Optional[str] = Query(None),
    path: str = Query(...),
    expires_in: Optional[int] = Query(None),
) -> Dict[str, Any]:
    require_api_key(req)
    ident = resolve_identity_from_params(user_id, session_id)

    rel = safe_rel_path(path)
    st = store_or_die()
    key = st.key_for(ident, rel)

    if not st.object_exists(key):
        raise HTTPException(status_code=404, detail="not found")

    url = st.presign_get(key=key, expires_in=expires_in)
    return {
        "status": "success",
        "path": rel,
        "expires_in": st.clamp_expires(expires_in, st.presign_default),
        "download_url": url,
    }
