# SeedIQ

SeedIQ is a multi-page seed intelligence and sales prototype backed by a working **read once → normalize once → reuse everywhere** farm-data architecture.

## Current connected stack

- **GitHub** — source of truth: `CalebWilson1121/seediq`
- **Vercel** — FastAPI + HTML deployment; GitHub pushes auto-deploy
- **Supabase Postgres** — normalized farm, field, crop, document, recommendation and AI-event data
- **Supabase Storage** — private `seediq-documents` bucket for original APH / MBAR / SOI source files
- **OpenAI** — optional explanation layer; automatically activates when `OPENAI_API_KEY` is present

Production health endpoint:

`/api/health`

Farm Data Hub:

`/data-hub.html`

## Architecture

```text
APH / MBAR / SOI
      ↓
Upload to SeedIQ API
      ↓
SHA-256 duplicate check
      ↓
Parser / normalizer
      ├── original source → private Supabase Storage
      └── normalized facts → Supabase Postgres
                           ↓
             farm / field context object
                 ├── seed placement engine
                 ├── coverage / APH analysis
                 ├── CRM / prospect workflows
                 └── compact context → AI explanation
```

The original document is not resent to AI every time. The source is read once, normalized and reused. AI is the explanation/narrative layer; deterministic engines and structured data remain the source of truth.

## Production database schema

Supabase migrations create:

- `farms`
- `fields`
- `documents`
- `crop_records`
- `source_facts`
- `seed_products`
- `field_profiles`
- `recommendations`
- `ai_events`

All application tables have RLS enabled. There are intentionally no browser-access policies yet; during development, data is accessed through the protected SeedIQ backend rather than directly from client-side JavaScript.

## Document ingestion

The current generic parser supports:

- CSV
- XLSX / XLSM
- JSON
- TXT
- text-based PDF

The repo contains sample APH / MBAR / SOI files. Production-grade carrier/AIP parsers still need to be built and validated against real de-identified forms.

### Read-once behavior

Each uploaded document is SHA-256 hashed before parsing/storage. If the same file is uploaded again, SeedIQ returns the existing document/farm reference rather than rereading and duplicating it.

## AI layer

`ai_service.py` has two providers:

1. `OpenAIProvider` — used automatically when `OPENAI_API_KEY` exists.
2. `MockAIProvider` — zero-cost fallback for development.

The OpenAI provider receives `compact_ai_context()` rather than the original source file. Default model is `gpt-5.6-luna`, override with `OPENAI_MODEL`.

Required Vercel secret to activate live AI:

```text
OPENAI_API_KEY
```

Do not place an API key in HTML, JavaScript or the GitHub repository.

## Local development

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn server:app --reload
```

Open:

`http://127.0.0.1:8000/data-hub.html`

When `POSTGRES_URL` is absent, SeedIQ automatically falls back to a local SQLite database for development.

## API endpoints

- `GET /api/health`
- `POST /api/documents/upload`
- `GET /api/farms`
- `GET /api/farms/{farm_id}/context`
- `POST /api/farms/{farm_id}/ai`
- `POST /api/seed/rank`

## Deployment

Vercel is linked directly to the GitHub repository. Pushing to `main` automatically creates a production deployment. Preview branches create Vercel preview deployments.

Supabase connection/storage variables are configured as Vercel environment variables. Secrets stay in Vercel/Supabase and are not committed to GitHub.

## Next product-development milestones

1. Carrier/AIP-specific APH, MBAR and SOI parsers with fixture tests.
2. NOAA weather enrichment by field/location.
3. NRCS soil enrichment by field polygon.
4. Seed-company genetics import and normalization.
5. Deterministic field-fit scoring and whole-farm portfolio optimization.
6. SeedIQ user authentication and organization/role controls before external production use.
