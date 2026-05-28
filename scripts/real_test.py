"""
Real end-to-end test with a large, realistic prompt.
Simulates what an AI coding agent actually sends: big code file + stack trace + RAG chunks.
"""
import httpx
import json
import textwrap

BASE = "http://127.0.0.1:8080"
HEADERS = {"x-api-key": "dc_dev_local", "Content-Type": "application/json"}

def separator(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)

# ── Build a realistic large prompt ────────────────────────────────────────────

BIG_CODE_FILE = '''
import os, sys, json, time, threading, logging
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 8080
    workers: int = 4
    timeout: float = 30.0
    retry_limit: int = 3
    db_path: Path = Path("/data/app.db")
    log_level: str = "INFO"
    enable_cache: bool = True
    cache_ttl: int = 3600
    max_connections: int = 100
    auth_required: bool = True
    secret_key: str = field(default_factory=lambda: os.environ.get("SECRET_KEY", "dev-secret"))

@dataclass
class RequestContext:
    request_id: str
    user_id: Optional[str]
    project_id: str
    timestamp: float
    metadata: Dict[str, Any] = field(default_factory=dict)

class DatabasePool:
    def __init__(self, path: Path, max_connections: int = 10):
        self.path = path
        self.max_connections = max_connections
        self._pool = []
        self._lock = threading.Lock()
        self._initialize()

    def _initialize(self):
        import sqlite3
        for _ in range(self.max_connections):
            conn = sqlite3.connect(str(self.path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._pool.append(conn)
        logger.info(f"Database pool initialized with {self.max_connections} connections")

    def acquire(self):
        with self._lock:
            if self._pool:
                return self._pool.pop()
            raise RuntimeError("No available database connections")

    def release(self, conn):
        with self._lock:
            self._pool.append(conn)

    def execute(self, query: str, params=()) -> List[Dict]:
        conn = self.acquire()
        try:
            cursor = conn.execute(query, params)
            conn.commit()
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            conn.rollback()
            logger.error(f"Database error: {e}")
            raise
        finally:
            self.release(conn)

class CacheLayer:
    def __init__(self, ttl: int = 3600):
        self._store: Dict[str, tuple] = {}
        self._ttl = ttl
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            if key in self._store:
                value, expires = self._store[key]
                if time.time() < expires:
                    return value
                del self._store[key]
        return None

    def set(self, key: str, value: Any):
        with self._lock:
            self._store[key] = (value, time.time() + self._ttl)

    def invalidate(self, key: str):
        with self._lock:
            self._store.pop(key, None)

    def clear(self):
        with self._lock:
            self._store.clear()

class RequestProcessor:
    def __init__(self, config: Config, db: DatabasePool, cache: CacheLayer):
        self.config = config
        self.db = db
        self.cache = cache
        self.executor = ThreadPoolExecutor(max_workers=config.workers)

    def process(self, ctx: RequestContext, payload: Dict[str, Any]) -> Dict[str, Any]:
        cache_key = f"{ctx.project_id}:{json.dumps(payload, sort_keys=True)}"
        cached = self.cache.get(cache_key)
        if cached:
            logger.debug(f"Cache hit for {ctx.request_id}")
            return cached

        result = self._run_pipeline(ctx, payload)
        self.cache.set(cache_key, result)
        self._record_usage(ctx, result)
        return result

    def _run_pipeline(self, ctx: RequestContext, payload: Dict) -> Dict:
        steps = [self._validate, self._enrich, self._transform, self._finalize]
        data = payload.copy()
        for step in steps:
            data = step(ctx, data)
        return data

    def _validate(self, ctx: RequestContext, data: Dict) -> Dict:
        required = ["model", "messages"]
        for field in required:
            if field not in data:
                raise ValueError(f"Missing required field: {field}")
        return data

    def _enrich(self, ctx: RequestContext, data: Dict) -> Dict:
        data["_context"] = {"request_id": ctx.request_id, "user": ctx.user_id}
        return data

    def _transform(self, ctx: RequestContext, data: Dict) -> Dict:
        data["messages"] = [m for m in data["messages"] if m.get("content")]
        return data

    def _finalize(self, ctx: RequestContext, data: Dict) -> Dict:
        data["_processed_at"] = time.time()
        return data

    def _record_usage(self, ctx: RequestContext, result: Dict):
        try:
            self.db.execute(
                "INSERT INTO usage (request_id, project_id, created_at) VALUES (?,?,?)",
                (ctx.request_id, ctx.project_id, time.time())
            )
        except Exception as e:
            logger.warning(f"Failed to record usage: {e}")
''' * 3  # repeat 3x to make it realistically large

STACK_TRACE = """
Traceback (most recent call last):
  File "/app/apps/api/decompute_gateway/main.py", line 263, in anthropic_messages_proxy
    comp = await yscompress.compress_messages(messages, mode=mode, ccr=True)
  File "/app/apps/api/decompute_gateway/yscompress.py", line 108, in compress_messages
    async with httpx.AsyncClient(timeout=self.settings.yscompress_timeout_s) as client:
  File "/usr/local/lib/python3.12/site-packages/httpx/_client.py", line 1678, in __aenter__
    return self
  File "/app/apps/api/decompute_gateway/yscompress.py", line 111, in compress_messages
    resp = await client.post(
  File "/usr/local/lib/python3.12/site-packages/httpx/_client.py", line 1761, in post
    return await self.request("POST", url, content=content, data=data, json=json, **kwargs)
  File "/usr/local/lib/python3.12/site-packages/httpx/_client.py", line 1522, in request
    return await self.send(request, auth=auth, follow_redirects=follow_redirects)
  File "/usr/local/lib/python3.12/site-packages/httpx/_client.py", line 1609, in send
    response = await self._send_with_response_stream(request, auth=auth, follow_redirects=follow_redirects)
httpx.ConnectError: [Errno 111] Connection refused
ERROR:    Exception in ASGI application
Traceback (most recent call last):
  File "/usr/local/lib/python3.12/site-packages/uvicorn/protocols/http/httptools_impl.py", line 411, in run_asgi
    result = await app(self.scope, self.receive, self.send)
RuntimeError: Unexpected ASGI error
""" * 5

RAG_CHUNKS = """
[Retrieved document 1 - score: 0.94]
Title: FastAPI Async Best Practices
Content: When building async FastAPI applications, always use async def for route handlers that perform I/O operations. Use httpx.AsyncClient for outbound HTTP requests. Avoid blocking calls inside async functions. Use asyncio.gather() for concurrent operations. Connection pools should be initialized at startup using lifespan events. Never call time.sleep() in async context - use asyncio.sleep() instead.

[Retrieved document 2 - score: 0.91]
Title: HTTPX Connection Error Handling
Content: httpx.ConnectError occurs when a connection attempt fails. Common causes: target service not running, wrong port, firewall blocking. Handle with try/except and implement exponential backoff. For microservice communication, use health checks before requests. Circuit breaker pattern recommended for production.

[Retrieved document 3 - score: 0.87]
Title: Python Async Context Managers
Content: AsyncClient should be used as a context manager (async with) to ensure proper connection cleanup. Alternatively, create a module-level client and close it in shutdown event. Shared clients are more efficient than creating new ones per request. Set appropriate timeouts to prevent hanging connections.

[Retrieved document 4 - score: 0.82]
Title: Microservice Communication Patterns
Content: Service-to-service calls should include retry logic, circuit breakers, and fallback strategies. Use health check endpoints before establishing connections. Implement graceful degradation when dependent services are unavailable. Log all inter-service communication failures with context.
""" * 4

messages = [
    {
        "role": "tool",
        "content": f"=== Source file: processor.py ===\n{BIG_CODE_FILE}"
    },
    {
        "role": "tool",
        "content": f"=== Error logs (last 500 lines) ===\n{STACK_TRACE}"
    },
    {
        "role": "tool",
        "content": f"=== RAG retrieval results ===\n{RAG_CHUNKS}"
    },
    {
        "role": "user",
        "content": "Based on the source code, stack trace, and retrieved docs above — what is causing the ConnectError and what is the cleanest fix? Give me the exact code change."
    }
]

raw_chars = sum(len(json.dumps(m)) for m in messages)
print(f"\n📦 Prompt size: {raw_chars:,} chars (~{raw_chars//4:,} tokens)")
print("   → 1 big code file (repeated 3×) + stack trace (repeated 5×) + RAG chunks (repeated 4×)")

# ── Step 1: Preview compression ───────────────────────────────────────────────
separator("STEP 1 — Compression preview (before hitting Claude)")
r = httpx.post(f"{BASE}/v1/compression/preview",
    headers=HEADERS,
    json={"messages": messages, "mode": "safe", "ccr": True},
    timeout=30)
preview = r.json()
print(f"  Tokens before : {preview['tokens_before']:,}")
print(f"  Tokens after  : {preview['tokens_after']:,}")
print(f"  Tokens saved  : {preview['tokens_saved']:,}")
print(f"  Ratio         : {preview['compression_ratio']:.4f}  ({(1-preview['compression_ratio'])*100:.1f}% reduction)")
print(f"  Engine        : {preview['engine']}")
print(f"  Transforms    : {preview['transforms_applied']}")
if preview.get('ccr_keys'):
    print(f"  CCR keys      : {len(preview['ccr_keys'])} originals stored locally")

# ── Step 2: Real Claude call through gateway ──────────────────────────────────
separator("STEP 2 — Real Claude call through the full stack")
print("  Sending to Claude (this uses real tokens)...")
r = httpx.post(f"{BASE}/v1/chat/completions",
    headers=HEADERS,
    json={
        "model": "claude-sonnet-4-5",
        "messages": messages,
        "max_tokens": 512
    },
    timeout=60)
resp = r.json()

print(f"\n  Claude's answer:\n")
answer = resp["choices"][0]["message"]["content"]
for line in answer.split("\n"):
    print(f"    {line}")

usage = resp.get("usage", {})
print(f"\n  Token usage (what Anthropic actually billed):")
print(f"    Raw prompt tokens    : {usage.get('raw_prompt_tokens', 'n/a'):,}")
print(f"    Sent to Claude       : {usage.get('prompt_tokens', 'n/a'):,}")
print(f"    Tokens saved         : {usage.get('tokens_saved', 'n/a'):,}")
print(f"    Output tokens        : {usage.get('completion_tokens', 'n/a'):,}")

ys = resp.get("yscompress", {})
if ys:
    print(f"\n  YSCompress:")
    print(f"    Engine  : {ys.get('engine')}")
    print(f"    Mode    : {ys.get('mode')}")

# ── Step 3: Usage summary ─────────────────────────────────────────────────────
separator("STEP 3 — Cumulative usage from gateway DB")
r = httpx.get(f"{BASE}/v1/usage", headers=HEADERS)
u = r.json()
print(f"  Total requests       : {u['requests']}")
print(f"  Raw input tokens     : {u['raw_input_tokens']:,}")
print(f"  Sent to Claude       : {u['compressed_input_tokens']:,}")
print(f"  Total tokens saved   : {u['tokens_saved']:,}")
print(f"  Savings              : {u['estimated_savings_percent']:.1f}%")

separator("✅ ALL SYSTEMS WORKING")
print("  Gateway    : http://127.0.0.1:8080")
print("  Dashboard  : http://127.0.0.1:8080/dashboard")
print("  YSCompress : http://127.0.0.1:8787")
print()
