# Mint.ai Visibility MCP Server

MCP server for analyzing brand visibility in LLM responses via the Mint.ai API.

**Version 4.1.0** — Hardened & Optimized: persistent HTTP client, input validation, prefixed tool names, tool annotations, English docs.

---

## Tools (7)

---

### 1. `mint_get_domains_and_topics`

Lists all domains (brands) and topics (markets). **Call first** to get IDs needed by other tools.

| Annotation | Value |
|---|---|
| readOnlyHint | true |
| idempotentHint | true |

**Examples:** "What brands do I have?", "List my topics"

**Returns:**
```json
{
  "domains": [...],
  "topics": [{"domainId": "...", "domainName": "IBIS", "topicId": "...", "topicName": "IBIS FR"}],
  "mapping": {"IBIS > IBIS FR": {"domainId": "694a...", "topicId": "694a..."}},
  "errors": []
}
```

---

### 2. `mint_get_topic_scores`

Day-by-day Brand vs Competitors scores for **one topic**, broken down by AI model.

| Param | Required | Description |
|---|---|---|
| `domainId` | yes | Domain ID |
| `topicId` | yes | Topic ID |
| `startDate` | no | YYYY-MM-DD (default: -30 days) |
| `endDate` | no | YYYY-MM-DD (default: today) |
| `models` | no | Comma-separated filter (e.g. `gpt-5.1,sonar-pro`) |

**Returns:** flat dataset `[{Date, EntityName, EntityType, Score, Model}, ...]`

---

### 3. `mint_get_scores_overview`

Average visibility score for **multiple topics** in one call. Self-contained: fetches topics via filters.

| Param | Required | Description |
|---|---|---|
| `brand_filter` | no | e.g. `IBIS`, `Mercure` |
| `market_filter` | no | e.g. `FR`, `UK` |
| `topic_ids` | no | Explicit topicId list |
| `startDate` / `endDate` | no | Default: -90 days |
| `models` | no | Comma-separated filter |

**Returns:** Markdown table + JSON rows with avg scores per topic.

---

### 4. `mint_get_visibility_trend`

**Binned time series** (day/week/month) for line charts.

| Param | Required | Description |
|---|---|---|
| `brand_filter` / `market_filter` / `topic_ids` | no | Topic selection |
| `startDate` / `endDate` | no | Default: Jan 1st current year to today |
| `models` | no | Model filter |
| `granularity` | no | `day` / `week` (default) / `month` |
| `aggregation` | no | `average` (default) / `per_topic` |

**Returns:**
```json
{
  "series": [{"name": "IBIS (week avg)", "points": [{"date": "2026-01-06", "score": 52.3, "n": 45}]}],
  "chart_hint": "line"
}
```

---

### 5. `mint_get_topic_sources`

Top cited domains and URLs for **one topic**, per AI model. Uses Mint's pre-aggregated API (fast).

| Param | Required | Description |
|---|---|---|
| `domainId` | yes | Domain ID |
| `topicId` | yes | Topic ID |
| `startDate` / `endDate` | no | Default: -90 days |
| `models` | no | Model filter |

**Returns:** `top_domains`, `top_urls`, `domains_over_time`, `urls_over_time`, `global_metrics`

---

### 6. `mint_get_raw_responses` (core)

Classifies every cited URL on **2 independent axes**:

**Axis 1 — ownership** (domain regex):
- `owned` — URL on a brand-owned domain (e.g. `all.accor.com`)
- `external` — third-party domain (e.g. `booking.com`)

**Axis 2 — brand_status** (via crawl enrichment):
- `own_only` — page mentions your brand only
- `own+comp` — page mentions your brand AND competitors
- `comp_only` — page mentions only competitors
- `no_brand` — crawl ran, no brand detected
- `not_enriched` — no crawl data

**3 modes** via `aggregate`:
- `classified` (default) — full enrichment + classification + cross matrix
- `sources` — top domains/URLs without enrichment (faster)
- `none` — raw responses for drill-down

| Param | Required | Description |
|---|---|---|
| `domainId` | yes | Domain ID |
| `topic_ids` / `brand_filter` / `market_filter` | no | Topic selection |
| `response_brand_mentioned` | no | `true` / `false` / `all` (default) |
| `ownership_filter` | no | `owned` / `external` / `all` (default) |
| `brand_status_filter` | no | String or list from the 5 statuses |
| `aggregate` | no | `classified` / `sources` / `none` |
| `top_n` | no | Max URLs (default: 30, max: 500) |

**Example queries:**
```json
// "External sources mentioning my brand"
{"domainId": "...", "ownership_filter": "external", "brand_status_filter": ["own_only", "own+comp"]}

// "When IBIS is NOT cited, who takes my place?"
{"domainId": "...", "response_brand_mentioned": "false", "aggregate": "sources"}
```

---

### 7. `mint_enrich_sources`

Direct batch URL enrichment: DataForSEO category + detected brands.

| Param | Required | Description |
|---|---|---|
| `domainId` | yes | Domain ID |
| `urls` | yes | URL list (max 1000) |
| `reportId` | yes | Report ID |
| `topicId` | no | Resolves market/language |
| `brand_name` | no | Adds owned/external classification |

> For typical analysis, prefer `mint_get_raw_responses(aggregate="classified")`.

---

## Installation

```bash
pip install -r requirements.txt
```

## Local launch

```bash
export MINT_API_KEY="mint_live_your_key_here"
uvicorn mcp_mint_server:app --host 0.0.0.0 --port 8000 --workers 1
```

Endpoints:
- `GET /health` — JSON healthcheck
- `GET /sse` — SSE connection (Claude.ai, web MCP clients)
- `POST /sse` + `POST /messages` — JSON-RPC messages

## Claude Desktop config

```json
{
  "mcpServers": {
    "mint-visibility": {
      "command": "python",
      "args": ["-m", "uvicorn", "mcp_mint_server:app", "--port", "8765"],
      "cwd": "/absolute/path/to/mint-mcp",
      "env": {
        "MINT_API_KEY": "mint_live_your_key_here"
      }
    }
  }
}
```

## Render deployment

1. Push to GitHub
2. Render: New Web Service, connect repo
3. Build: `pip install -r requirements.txt`
4. Start: `uvicorn mcp_mint_server:app --host 0.0.0.0 --port $PORT --workers 1`
5. Set `MINT_API_KEY` in Environment
6. Deploy

Connect in Claude.ai: `https://<your-app>.onrender.com/sse`

## Project structure

```
.
├── mcp_mint_server.py   # Complete MCP server (single file)
├── owned_domains.json   # Brand → owned domains mapping
├── requirements.txt     # Dependencies
├── .gitignore
└── README.md
```

## Owned Domains config

`owned_domains.json` maps each brand to its owned domains. This provides the `ownership` axis independently of the crawl enrichment.

```json
{
  "IBIS":     ["accor.com"],
  "Fairmont": ["accor.com", "fairmont.com"],
  "_default": ["accor.com"]
}
```

To add a brand: edit the file and restart.

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `MINT_API_KEY` | yes | — | Mint.ai API key |
| `MINT_BASE_URL` | no | `https://api.getmint.ai/api` | Base URL |
| `HTTP_TIMEOUT` | no | `30` | Timeout (seconds) |
| `HTTP_MAX_CONCURRENT` | no | `8` | Max concurrent API requests |
| `OWNED_DOMAINS_PATH` | no | `./owned_domains.json` | Mapping file path |

## Backward compatibility

v4.1.0 tool names are prefixed with `mint_` (e.g. `mint_get_topic_scores`). The old unprefixed names (e.g. `get_topic_scores`) still work as aliases — no breaking change for existing clients.

## Changelog

### v4.1.0 (2026-04-20) — Hardened & Optimized

- **PERF**: Persistent `httpx.AsyncClient` with connection pooling and TLS reuse
- **PERF**: Starlette `on_startup`/`on_shutdown` lifecycle for HTTP client management
- **ROBUST**: Input validation on all tool arguments with clear error messages
- **ROBUST**: Tool names prefixed with `mint_` to avoid collisions
- **ROBUST**: Tool annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`)
- **ROBUST**: New `InvalidInput` error type caught in dispatcher
- **DOCS**: Tool descriptions rewritten in English with USE FOR / DON'T USE FOR
- **DOCS**: README rewritten in English
- **FIX**: Server name follows Python convention (`mint_visibility_mcp`)
- **COMPAT**: Backward-compatible aliases for v4.0.0 unprefixed tool names
- **COMPAT**: SSE transport preserved (Render + Claude.ai compatible)
- **COMPAT**: Same deploy process — just push and go

### v4.0.0 (2026-04-16) — Sources Classification

Major rewrite. Not backward-compatible with v3.x.

- 7 specialized tools (from 4)
- 2-axis classification (ownership + brand_status)
- `get_visibility_trend` for temporal charts
- `get_raw_responses` with 3 modes
- `owned_domains.json` external config
- Strict `(reportId, url)` coupling (v3.x bug fix)
- Typed errors, exponential retry, global semaphore
- `/health` endpoint

### v3.6.0 — v3.0.0

See previous releases for v3.x changelog.
