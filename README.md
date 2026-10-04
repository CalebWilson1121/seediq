# SeedIQ Farm Data Engine Starter

This repo combines the SeedIQ multi-page prototype with the first working backend for the **read once → normalize → reuse** architecture.

## Fastest way to run

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn server:app --reload
```

Then open:

`http://127.0.0.1:8000/data-hub.html`

Do **not** just double-click `data-hub.html` if you want document upload/API functionality. The other prototype pages still work as static HTML.


## If you see `405 Not Allowed`

That means the frontend is being served by a static host that is **not running the Python API**. It is not an APH parsing failure. Use the included `render.yaml`, `Dockerfile`, or run `uvicorn server:app` so the same site serves both the HTML pages and `/api/*`. See `DEPLOY.md`.

## Test it

Upload one of the included sample files:

- `sample_aph.csv`
- `sample_mbar.csv`
- `sample_soi.csv`

The engine will:

1. hash and store the original source,
2. detect/type the document,
3. parse recognized columns,
4. create/update the farm and fields,
5. store crop records,
6. store source facts with row-level provenance,
7. expose a reusable farm context at `/api/farms/{id}/context`.

## Important files

- `server.py` — API and page server.
- `database.py` — normalized database schema.
- `parsers.py` — parser registry and current generic mappings.
- `ingestion.py` — read-once ingestion pipeline.
- `context_builder.py` — full farm context + compact AI context.
- `ai_service.py` — replaceable AI provider boundary; mock provider costs $0.
- `seed_engine.py` — deterministic seed scoring example.
- `ARCHITECTURE.md` — architecture and next milestones.
- `data-hub.html` — interactive data-ingestion page.

## Current limitations

The parser is intentionally a starter. Real APH/MBAR/SOI formats vary by AIP, form version and export method. The production version should use actual de-identified examples and form-specific parser tests. Text-based PDFs can be read; scanned PDFs need a separate OCR/document-vision pipeline.

The included AI provider is a mock. That is deliberate: it proves the desired boundary before adding a paid API. When a real provider is added, it should receive `compact_ai_context()` instead of the original document whenever possible.
