# AcreFit Farm Data Engine — Architecture v0.1

## Core rule

**Read once → normalize once → store with provenance → reuse everywhere.**

Raw APH, MBAR, SOI and acreage documents are source evidence, not the runtime data model. The platform extracts useful values once and turns them into a stable farm/field/crop schema.

## Flow

```text
APH / MBAR / SOI / XLSX / CSV / PDF
              |
              v
      Document ingestion
      - SHA-256 dedupe
      - immutable source file
      - document type detection
              |
              v
         Parser registry
      - structured tabular parser
      - generic PDF/text parser
      - future carrier-specific parsers
              |
              v
       Normalization layer
      - farm
      - field
      - crop record
      - policy / coverage values
      - source facts + provenance
              |
              v
         Farm database
      SQLite in starter repo
      PostgreSQL/PostGIS in production
         /           \
        v             v
 deterministic      compact AI
 analytics          context only
        |             |
        +------v------+
          AcreFit apps
```

## Why this is cheaper

AI does not repeatedly ingest full source documents. A source file is parsed once. Later AI tasks receive a compact JSON context containing only relevant normalized values.

## Why this is more reliable

The database is the source of truth. A value such as approved APH is persisted once and can be traced to the document and row/page that created it. Reopening a farm does not ask a language model to reinterpret the same source.

## Current starter capabilities

- Upload endpoint for CSV, XLSX, JSON, TXT and text-based PDF.
- SHA-256 duplicate detection.
- APH / MBAR / SOI document-type detection.
- Header alias mapping for structured files.
- Normalized farms, fields and crop records.
- Source-fact/provenance table.
- Compact AI context builder.
- Zero-cost mock AI provider showing the intended API boundary.
- Deterministic seed-fit ranking engine kept outside AI.
- `data-hub.html` UI to upload and inspect data.

## Next production milestones

1. Obtain real de-identified APH, MBAR and SOI samples from each AIP/carrier.
2. Create one parser per known form/version.
3. Add field-level reconciliation rules across APH vs MBAR vs SOI.
4. Add validation queue when two source documents disagree.
5. Move persistence to PostgreSQL/PostGIS.
6. Store field geometry and connect NRCS/NOAA enrichment jobs.
7. Add permissions by seed company, dealership, rep and agronomist.
8. Add real AI provider behind `AIProvider`; keep the provider replaceable.
9. Add prompt/context budgets and usage logging.
10. Add encryption, audit logs, retention rules and secure object storage before handling production customer documents.

## Important production principle

A language model should never silently overwrite a normalized value. If AI is used to interpret an ambiguous source, the extracted value should enter a reviewable state with source location and confidence before becoming authoritative.
