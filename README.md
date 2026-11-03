# Lio Procurement App

Upload vendor-offer PDFs, extract the details with AI, and track procurement
requests across your organization.

- **`frontend/`** — Next.js 15, React 19, Tailwind CSS 4
- **`backend/`** — FastAPI, SQLAlchemy, Alembic, JWT auth
- **Database** — PostgreSQL 16

## Prerequisites

- **Docker Desktop** (or Docker Engine) with **Docker Compose v2** — running before you start.
- An **OpenAI API key** is optional: the app boots and runs without it; only PDF extraction needs one.

## Quick start (Docker)

```bash
cp .env.example .env      # optional: add your OpenAI API key for PDF extraction
docker compose up --build
```

| | URL |
| --- | --- |
| App | http://localhost:3200 |
| API + docs | http://localhost:7200 · http://localhost:7200/docs |

Sign in with a demo account — no registration needed (password `demo1234`).
Development happens in the **Acme Corp** organization, which has one of each role:

| Account | Role | Can do |
| --- | --- | --- |
| `admin@acme.com` | admin | everything incl. members & settings |
| `buyer@acme.com` | buyer | approve requests |
| `requester@acme.com` | requester | create & track requests |

Every request needs approval by a buyer or admin (see the **Approvals** tab),
unless the organization approves small requests automatically (Acme: below
€2,000 net, set under **Organization → Settings**).

Other tenants exist to show the multi-tenant setup — **Walmart, Schaeffler,
Siemens, BMW** — each with an admin at `admin@<company>.com` (same password).
Their data is fully isolated from Acme's.

On startup the backend applies schema migrations and seeds the database (demo
accounts, organizations, commodity groups, suppliers) — both steps are
idempotent, so restarts never duplicate or overwrite anything (your data lives
in the `postgres_data` volume; `docker compose down -v` resets it). The OpenAI
key is only needed for PDF extraction — everything else works without it.

## Per-organization setup

Customer-specific behaviour is data, not code: it lives in each organization's
settings (JSON, validated by `OrganizationSettings`) or in rows that belong to
the organization. Acme's setup is seeded in `backend/app/seed.py`.

| Setting | Acme | Changed via |
| --- | --- | --- |
| Required request fields | none | Organization → Settings |
| Automatic approval below (net) | €2,000 | Organization → Settings |
| Booking rules (keywords → commodity group) | cable ties → Electrical Engineering, printer toner → IT Hardware, small software subscriptions → Office Supplies | seed / API |
| Own commodity groups | "Office Supplies" (only visible to Acme) | seed |
| Catalog (suppliers, articles, negotiated prices) | yes | seed |

## Local development

```bash
docker compose up db        # Postgres only

# backend  → http://localhost:7200
cd backend
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e .
alembic upgrade head        # schema
python -m app.seed          # reference data (idempotent)
uvicorn app.main:app --reload --port 7200

# frontend → http://localhost:3200
cd frontend
npm install
npm run dev
```

## Tests

```bash
cd backend && python -m pytest      # API tests (in-memory SQLite)
cd frontend && npx tsc --noEmit && npm run build
```

### Extraction eval

The five sample quotes are graded against an answer key
(`backend/evals/ground_truth.json`, 8 checks per quote). The PDFs aren't in the
repo because they contain personal data; put them in a folder and run, with
`OPENAI_API_KEY` set and the stack up:

```bash
docker compose cp <quotes-folder> backend:/tmp/quotes
docker compose exec backend python -m evals.run_eval /tmp/quotes --runs 3
```

## Layout

```
backend/    FastAPI app — routers, services, models, schemas, core; Alembic migrations
frontend/   Next.js app — app-router pages, typed API client (src/lib/api), components
docker-compose.yml
```
