# Decompute Gateway V0 Production Runbook

## Bootstrap

1. Provision Postgres (managed: RDS, Cloud SQL, Neon, Supabase; or self-hosted).
2. Generate a Fernet CCR encryption key:
   ```bash
   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
3. Set production env vars from `.env.production.example` — at minimum:
   ```bash
   DECOMPUTE_ENV=production
   DECOMPUTE_DATABASE_URL=postgresql+psycopg://user:pass@host:5432/decompute
   DECOMPUTE_CCR_ENCRYPTION_KEY=<fernet-key>
   DECOMPUTE_API_KEY_HASH_SALT=<random-32-char-string>
   ANTHROPIC_API_KEY=sk-ant-...
   DECOMPUTE_REQUIRE_AUTH=true
   DECOMPUTE_DASHBOARD_AUTH_REQUIRED=true
   DECOMPUTE_CORS_ORIGINS=https://your-domain.com
   ```
4. Run migrations against the blank database:
   ```bash
   alembic upgrade head
   ```
5. Bootstrap the first admin key (shown once — store in a password manager):
   ```bash
   python scripts/bootstrap_admin.py --name "Founder Admin"
   ```
6. Start the gateway:
   ```bash
   bash scripts/run_prod.sh
   # or: docker-compose -f docker-compose.v0.yml up
   ```

## Migrating from the old SQLite dev schema

The V0 P0 patch replaces the hand-rolled `sqlite3` schema with SQLAlchemy + Alembic.
Old dev databases do not have Alembic version tracking, so `alembic upgrade head` will
fail with "table already exists".

**Fix (local dev only — no production data at this point):**
```bash
rm .decompute/decompute.db
# Gateway recreates the schema automatically on next start via create_schema_for_local_dev()
make dev
```

For production: the old schema was never used in production, so this is a fresh deploy.
Always run `alembic upgrade head` on a blank Postgres database.

## Adding a new migration

```bash
alembic revision -m "describe_the_change"
# Edit alembic/versions/<rev>_describe_the_change.py
# Implement upgrade() and downgrade()
alembic upgrade head
```

Always implement `downgrade()` so `alembic downgrade -1` works in rollback.

## Health

- `/livez` — process is alive (no DB check).
- `/readyz` — DB is reachable and Anthropic credentials are configured. Use for load-balancer health checks.
- `/metrics` — Prometheus scrape target.

## Backup

Use your managed Postgres provider's snapshot tooling. For self-hosted:

```bash
pg_dump "$DECOMPUTE_DATABASE_URL" > backup_$(date +%Y%m%d_%H%M%S).sql
```

Automate with a cron job or pg_cron. Store backups off-host (S3, GCS).

## Restore

```bash
psql "$DECOMPUTE_DATABASE_URL" < backup_YYYYMMDD_HHMMSS.sql
alembic upgrade head   # ensures schema is at latest revision after restore
```

## Rollback

1. Stop new traffic (take instance out of load balancer rotation).
2. Deploy the previous Docker image.
3. If the schema needs to roll back:
   ```bash
   alembic downgrade -1
   ```
   Only do this after confirming the migration implements a safe `downgrade()`.
4. Verify `/readyz`, `/v1/models`, and one mock `/v1/messages` request before restoring traffic.

## Connection pool sizing

Defaults (`db.py`):
- `DECOMPUTE_DB_POOL_SIZE=5` — connections kept open per worker
- `DECOMPUTE_DB_MAX_OVERFLOW=10` — extra burst connections allowed

For Gunicorn with `workers = 2 * cpu + 1`, set pool_size to 3–5 and max_overflow to 10.
Total max connections = `workers × (pool_size + max_overflow)`.

## Incident checklist

For any failed request, collect:

- `x-decompute-request-id` (response header)
- `request-id` (upstream Anthropic request ID)
- project ID and API key prefix
- model name
- route JSON (from `x-decompute-route` or usage export)
- raw, YSCompress, Yellowstone final, output, cache tokens
- status code and latency_ms (both in usage_events table)
- check `audit_logs` for any key/config changes around the incident time
