"""
Large realistic prompt test — simulates a full AI coding agent session:
- Multiple source files
- Long conversation history
- Search results
- Build logs
- Git diff
"""
import httpx, json

BASE = "http://127.0.0.1:8080"
HEADERS = {"x-api-key": "dc_dev_local", "Content-Type": "application/json"}

def sep(t): print(f"\n{'='*60}\n  {t}\n{'='*60}")

# ── Simulate a large real agent session ───────────────────────────────────────

AUTH_MODULE = '''
import jwt, bcrypt, redis, logging, os, time
from datetime import datetime, timedelta
from typing import Optional
from functools import wraps
from flask import request, jsonify, g
from models import User, Session, AuditLog
from database import db_session
from config import settings

logger = logging.getLogger(__name__)
redis_client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)

SECRET_KEY = os.environ["JWT_SECRET_KEY"]
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
REFRESH_TOKEN_EXPIRE_DAYS = 7
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_DURATION = 900  # 15 minutes

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())

def create_access_token(user_id: str, scopes: list[str]) -> str:
    payload = {
        "sub": user_id,
        "scopes": scopes,
        "iat": datetime.utcnow(),
        "exp": datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        "type": "access"
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def create_refresh_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "iat": datetime.utcnow(),
        "exp": datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
        "type": "refresh"
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise AuthError("Token has expired")
    except jwt.InvalidTokenError as e:
        raise AuthError(f"Invalid token: {e}")

def check_rate_limit(user_id: str) -> bool:
    key = f"login_attempts:{user_id}"
    attempts = redis_client.get(key)
    if attempts and int(attempts) >= MAX_LOGIN_ATTEMPTS:
        ttl = redis_client.ttl(key)
        raise AuthError(f"Account locked. Try again in {ttl} seconds.")
    return True

def record_failed_attempt(user_id: str):
    key = f"login_attempts:{user_id}"
    pipe = redis_client.pipeline()
    pipe.incr(key)
    pipe.expire(key, LOCKOUT_DURATION)
    pipe.execute()

def clear_rate_limit(user_id: str):
    redis_client.delete(f"login_attempts:{user_id}")

class AuthError(Exception):
    pass

def require_auth(scopes: list[str] = []):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            auth_header = request.headers.get("Authorization")
            if not auth_header or not auth_header.startswith("Bearer "):
                return jsonify({"error": "Missing authorization header"}), 401
            token = auth_header.split(" ")[1]
            try:
                payload = decode_token(token)
                if payload.get("type") != "access":
                    return jsonify({"error": "Invalid token type"}), 401
                for scope in scopes:
                    if scope not in payload.get("scopes", []):
                        return jsonify({"error": f"Missing scope: {scope}"}), 403
                g.user_id = payload["sub"]
                g.scopes = payload["scopes"]
            except AuthError as e:
                return jsonify({"error": str(e)}), 401
            return f(*args, **kwargs)
        return decorated
    return decorator

def login(email: str, password: str) -> dict:
    user = db_session.query(User).filter_by(email=email.lower()).first()
    if not user:
        time.sleep(0.3)  # Prevent timing attacks
        raise AuthError("Invalid credentials")
    check_rate_limit(str(user.id))
    if not verify_password(password, user.password_hash):
        record_failed_attempt(str(user.id))
        raise AuthError("Invalid credentials")
    if not user.is_active:
        raise AuthError("Account disabled")
    clear_rate_limit(str(user.id))
    session = Session(user_id=user.id, created_at=datetime.utcnow())
    db_session.add(session)
    db_session.commit()
    AuditLog.record(user_id=user.id, action="login", ip=request.remote_addr)
    return {
        "access_token": create_access_token(str(user.id), user.scopes),
        "refresh_token": create_refresh_token(str(user.id)),
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    }
''' * 2

BUILD_LOG = """
[2026-05-28 14:23:01] INFO  Starting build pipeline v2.4.1
[2026-05-28 14:23:01] INFO  Loading configuration from .env.production
[2026-05-28 14:23:02] INFO  Checking dependencies...
[2026-05-28 14:23:02] INFO  Installing packages from requirements.txt
[2026-05-28 14:23:02] DEBUG pip install --no-cache-dir -r requirements.txt
[2026-05-28 14:23:15] INFO  All packages installed successfully
[2026-05-28 14:23:15] INFO  Running pre-build checks...
[2026-05-28 14:23:15] INFO  Running mypy type checks...
[2026-05-28 14:23:22] ERROR mypy: error: auth/middleware.py:47: error: Argument 1 to "decode" has incompatible type "Optional[str]"; expected "str"
[2026-05-28 14:23:22] ERROR mypy: error: auth/middleware.py:52: error: Item "None" of "Optional[User]" has no attribute "id"
[2026-05-28 14:23:22] ERROR mypy: error: auth/middleware.py:61: error: Argument "user_id" to "create_access_token" has incompatible type "int"; expected "str"
[2026-05-28 14:23:22] ERROR mypy: Found 3 errors in 1 file (checked 24 source files)
[2026-05-28 14:23:22] FATAL Build failed at type check stage
[2026-05-28 14:23:22] INFO  Build duration: 21.4s
[2026-05-28 14:23:22] INFO  Exit code: 1
[2026-05-28 14:23:22] ERROR Pipeline terminated — rolling back deployment
[2026-05-28 14:23:23] INFO  Previous version v2.4.0 remains active
""" * 8

GIT_DIFF = """
diff --git a/auth/middleware.py b/auth/middleware.py
index a3f2b1c..d8e4921 100644
--- a/auth/middleware.py
+++ b/auth/middleware.py
@@ -44,12 +44,14 @@ class AuthMiddleware:
     def authenticate(self, request: Request) -> Optional[User]:
         token = request.headers.get("Authorization", "").removeprefix("Bearer ")
-        payload = jwt.decode(token, settings.SECRET_KEY)
+        if not token:
+            return None
+        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
         user_id = payload.get("sub")
         if not user_id:
             return None
-        user = self.db.query(User).get(user_id)
+        user = self.db.query(User).get(int(user_id))
         if not user or not user.is_active:
             return None
-        request.state.user = user
+        request.state.user_id = str(user.id)
         return user
@@ -61,7 +63,7 @@ class AuthMiddleware:
     def create_session_token(self, user: User) -> str:
-        return create_access_token(user.id, user.scopes)
+        return create_access_token(str(user.id), user.scopes)
""" * 3

SEARCH_RESULTS = """
[Search result 1 — score 0.96] Stack Overflow: jwt.decode() requires bytes or string, not None
When calling jwt.decode(), ensure the token argument is never None. Use: token = header.removeprefix("Bearer ") or "" to guarantee a string. jwt.decode("", key) raises DecodeError which is catchable, whereas jwt.decode(None, key) raises TypeError which is not.

[Search result 2 — score 0.93] PyJWT docs: Decoding tokens
jwt.decode(token: str, key: str, algorithms: List[str]) -> dict
The algorithms parameter is required since PyJWT 2.0. Not passing it raises DecodeWarning and will error in future versions. Always pass algorithms=["HS256"] or your specific algorithm explicitly.

[Search result 3 — score 0.91] GitHub issue #847: mypy "Argument 1 incompatible type Optional[str]"
This error occurs when a function expects str but receives Optional[str]. Fix: add a None check before calling the function, or use assert token is not None, or use the walrus operator: if token := header.get("Authorization"): ...

[Search result 4 — score 0.88] Real Python: Type narrowing in Python
mypy performs type narrowing after isinstance() and None checks. After `if token:`, mypy knows token is str (not Optional[str]) within that block. This eliminates Optional[str] errors without casting.

[Search result 5 — score 0.85] FastAPI security: OAuth2 bearer tokens
Standard pattern for extracting bearer tokens: use OAuth2PasswordBearer dependency or manually: auth = request.headers.get("Authorization"); token = auth.split(" ")[1] if auth and auth.startswith("Bearer ") else None
""" * 3

CONVERSATION_HISTORY = [
    {"role": "user", "content": "The CI build is failing on the auth module. Can you look at the logs?"},
    {"role": "assistant", "content": "I can see 3 mypy errors in auth/middleware.py. The main issues are: (1) passing Optional[str] where str is expected for jwt.decode, (2) accessing .id on Optional[User], and (3) passing int instead of str to create_access_token. Let me look at the full file."},
    {"role": "user", "content": "Here's the full middleware.py file and the git diff of recent changes."},
    {"role": "assistant", "content": "Looking at the diff, the issue is clear. The recent refactor changed `user.id` to `str(user.id)` in create_session_token but missed fixing the jwt.decode call and the None guard on the token. I'll search for the correct PyJWT 2.x API to confirm the fix."},
    {"role": "user", "content": "I've added the search results. What's the exact fix?"},
]

messages = [
    *CONVERSATION_HISTORY,
    {"role": "tool", "content": f"=== auth/auth.py ===\n{AUTH_MODULE}"},
    {"role": "tool", "content": f"=== CI build log (last 200 lines) ===\n{BUILD_LOG}"},
    {"role": "tool", "content": f"=== git diff HEAD~1 ===\n{GIT_DIFF}"},
    {"role": "tool", "content": f"=== Search results ===\n{SEARCH_RESULTS}"},
    {"role": "user", "content": "Based on all of the above — give me the exact 3-line fix for middleware.py that passes mypy and doesn't break existing tests."},
]

raw_chars = sum(len(json.dumps(m)) for m in messages)
print(f"\n📦 Prompt: {raw_chars:,} chars (~{raw_chars//4:,} tokens)")
print(f"   → {len(CONVERSATION_HISTORY)} message history + 2 code files + build log + git diff + search results")

# Step 1: Compression preview
sep("STEP 1 — Compression (YSCompress Rust core)")
r = httpx.post(f"{BASE}/v1/compression/preview", headers=HEADERS,
    json={"messages": messages, "mode": "safe", "ccr": True}, timeout=30)
p = r.json()
print(f"  Engine          : {p['engine']}")
print(f"  Tokens before   : {p['tokens_before']:,}")
print(f"  Tokens after    : {p['tokens_after']:,}")
print(f"  Tokens saved    : {p['tokens_saved']:,}")
print(f"  Reduction       : {(1 - p['compression_ratio'])*100:.1f}%")
print(f"  Transforms      : {p['transforms_applied']}")

# Step 2: Real Claude call
sep("STEP 2 — Real Claude answer")
r = httpx.post(f"{BASE}/v1/chat/completions", headers=HEADERS,
    json={"model": "claude-sonnet-4-5", "messages": messages, "max_tokens": 600},
    timeout=60)
resp = r.json()
answer = resp["choices"][0]["message"]["content"]
print()
for line in answer.split("\n"):
    print(f"  {line}")

usage = resp.get("usage", {})
print(f"\n  Billed by Anthropic:")
print(f"    Prompt tokens sent : {usage.get('prompt_tokens','n/a'):,}")
print(f"    Raw (uncompressed) : {usage.get('raw_prompt_tokens','n/a'):,}")
print(f"    Output tokens      : {usage.get('completion_tokens','n/a'):,}")
print(f"    Tokens saved       : {usage.get('tokens_saved','n/a'):,}")

# Step 3: Totals
sep("STEP 3 — Cumulative savings")
r = httpx.get(f"{BASE}/v1/usage", headers=HEADERS)
u = r.json()
print(f"  Total requests     : {u['requests']}")
print(f"  Total raw tokens   : {u['raw_input_tokens']:,}")
print(f"  Total sent         : {u['compressed_input_tokens']:,}")
print(f"  Total saved        : {u['tokens_saved']:,}")
print(f"  Overall savings    : {u['estimated_savings_percent']:.1f}%")
sep("✅ DONE")
