# Hosting

## Docker

```bash
cp .env.example .env
docker compose up --build
```

## Single VM

```bash
git clone <your-repo>
cd decompute_claude_gateway
cp .env.example .env
make setup
DECOMPUTE_HOST=0.0.0.0 make dev
```

For production, run with systemd or a process manager and put Caddy/Nginx in front with TLS.

## Environment for hosted mode

```bash
DECOMPUTE_REQUIRE_AUTH=true
DECOMPUTE_API_KEY=<admin-dev-key>
DECOMPUTE_MOCK_ANTHROPIC=false
ANTHROPIC_API_KEY=sk-ant-...
DECOMPUTE_RETURN_PROXY_METADATA=false
DECOMPUTE_PROMPT_CACHE=auto
```

## Multi-tenant roadmap

This bundle uses SQLite and simple API keys. Replace with Postgres + Alembic before real multi-tenant production.

Production modules to add:

- signup/login;
- organizations/projects;
- API key scopes;
- billing events;
- invoices/quotas;
- customer-managed provider keys;
- audit/event export;
- team dashboard.
