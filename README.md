# Mint.ai Visibility MCP Server

MCP server that exposes Mint.ai brand-visibility data (scores, share of voice, cited sources, page enrichment) to any MCP-compatible client — Claude Desktop, Claude.ai (SSE), or a custom agent.

**Version 5.2.0** — Owned-domain fix (corporate domains like `accor.com` are now owned on every topic), request throttling to protect the API, and a leaner `mint_get_topic_overview`. Builds on v5.1.0 (macro `mint_get_topic_overview`) and v5.0.0 (split source tools).

---

## Tool map (9 exposed)

| # | Tool | Level | One-liner |
|---|------|-------|-----------|
| 1 | `mint_get_domains_and_topics` | discovery | List brands (domains) + markets (topics) and their IDs. **Call first.** |
| 2 | `mint_get_models_by_topic` | discovery | List the AI models available for one topic. |
| 3 | `mint_get_topic_overview` | **macro** | One-call snapshot: score, share of voice, rank, competitors, top mentions. |
| 4 | `mint_get_topic_scores` | detail | Day-by-day Brand vs Competitors scores, per model. |
| 5 | `mint_get_scores_overview` | macro | Average score for **many** topics in one table. |
| 6 | `mint_get_visibility_trend` | detail | Binned time-series for charts. |
| 7 | `mint_get_topic_sources` | detail | Top cited domains/URLs (per model, over time). |
| 8 | `mint_get_response_sources` | **macro (sources)** | Fast cited-source overview, citation-weighted, owned vs external. No crawl. |
| 9 | `mint_enrich_cited_sources` | **detail (sources)** | Crawls pages (DataForSEO); ranks external sources by how much they cite your brand. |

Design principle: every area has a **macro** entry (fast, one call) and a **detail** tool. The macro tools point to the detail tools via a `next_step` field. `mint_get_raw_responses` and `mint_enrich_sources` remain callable as **unlisted backward-compat aliases**.

---

## Tools

### 1. `mint_get_domains_and_topics`
Lists every domain (brand) and topic (market) with their IDs. Every other tool needs a `domainId` / `topicId` from here.

- **Params:** none.
- **Returns:** `domains` (id+name), `topics` (domainId, domainName, topicId, topicName), a `"Brand > Topic" → IDs` mapping, `errors`.

### 2. `mint_get_models_by_topic`
The AI models tracked for one topic (resolved live — each topic can differ).

| Param | Req | Description |
|---|---|---|
| `domainId` | yes | from tool 1 |
| `topicId` | yes | from tool 1 |

Use the returned names in the `models` param of other tools (comma-separated, no spaces). By default other tools answer with the GLOBAL (all-models) view; offer a per-model deep dive only on request.

### 3. `mint_get_topic_overview` — macro snapshot
One call to `/visibility/aggregated`, no per-model fan-out. Headline KPIs only; the heavy time-series and full domain/URL lists are intentionally left to the detail tools.

| Param | Req | Description |
|---|---|---|
| `domainId`, `topicId` | yes | target topic |
| `startDate` / `endDate` | no | default last 30 days |
| `models` | no | omit = GLOBAL |
| `top_n` | no | rows for `top_mentions` (default 10) |
| `include_model_breakdown` | no | per-model score arrays (default true) |
| `useAllModelsForCompetitors` | no | count missing models as 0 (default false) |

**Returns:** `kpis` (averageScore, scoreVariation, brand_rank, entities_ranked, `share_of_voice` {latest, average, change…}, totals), `available_models`, `model_breakdown`, `competitors` [{name, averageScore, variation}], `top_mentions` (share of voice by mention count), `next_step`.

> Surfaces `shareOfVoice` and `topMentions` that no other tool exposes. (The always-zero `domainSourceAnalysis` block was removed in v5.2.0.)

### 4. `mint_get_topic_scores` — detail
Day-by-day Brand vs Competitors scores for one topic, broken down by AI model (GLOBAL + each model).

| Param | Req | Description |
|---|---|---|
| `domainId`, `topicId` | yes | target topic |
| `startDate` / `endDate` | no | default last 30 days |
| `models` | no | comma-separated filter |

**Returns:** flat `dataset` `[{Date, EntityName, EntityType, Score, Model}, …]`.

### 5. `mint_get_scores_overview` — multi-topic table
Average score per topic over a period, as a Markdown table + JSON rows. Resolves topics itself from filters.

| Param | Req | Description |
|---|---|---|
| `brand_filter` / `market_filter` / `topic_ids` | no | topic selection |
| `startDate` / `endDate` | no | default last 90 days |
| `models` | no | filter |

### 6. `mint_get_visibility_trend` — chart series
Binned (day/week/month) time-series shaped for line charts.

| Param | Req | Description |
|---|---|---|
| `brand_filter` / `market_filter` / `topic_ids` | no | selection |
| `granularity` | no | day / **week** / month |
| `aggregation` | no | **average** / per_topic |
| `startDate` / `endDate` / `models` | no | filters |

### 7. `mint_get_topic_sources` — detail sources
Top cited domains and URLs for one topic, per model and over time (Mint's pre-aggregated API, fast). Answers "who is cited / how often", not "does the page mention my brand".

| Param | Req | Description |
|---|---|---|
| `domainId`, `topicId` | yes | target topic |
| `startDate` / `endDate` / `models` | no | filters |

**Returns:** `top_domains`, `top_urls`, `domains_over_time`, `urls_over_time`, `global_metrics`.

### 8. `mint_get_response_sources` — fast source overview *(no crawl)*
Reads raw LLM responses and reports which sources are cited, split **owned vs external** and by **whether your brand was named in the answer**. All counts are **citation-weighted** (a URL cited 80× counts 80). No DataForSEO.

| Param | Req | Description |
|---|---|---|
| `domainId` | yes | target domain |
| `topic_ids` / `brand_filter` / `market_filter` | no | topic selection (default: all topics of the domain) |
| `response_brand_mentioned` | no | `true` / `false` / `all` — `false` answers "who replaces me" |
| `ownership_filter` | no | `owned` / `external` / `all` |
| `startDate` / `endDate` / `models` / `latestOnly` | no | filters |
| `top_n` | no | rows per ranking (default 30) |

**Returns:** `top_domains`, `top_urls` (carry `report_ids`), `top_of_mind`, `ownership_summary`, `brand_mentioned_split`, `matrix` (ownership × response_brand_mentioned), and `next_step.sources` (external URLs ready to enrich).

> Owned/external relies on `owned_domains.json` (see Config). Without it, everything is `external`.

### 9. `mint_enrich_cited_sources` — deep source enrichment *(crawl)*
Crawls cited pages via **DataForSEO** to detect brand vs competitors **in the page content**, and ranks external sources by **how much each one cites your brand** (`brand_citation_ranking`). Slower/paid — use after tool 8.

| Param | Req | Description |
|---|---|---|
| `domainId` | yes | target domain |
| `sources` | no | EXPLICIT mode: `[{url, reportId, topicId?}]` (e.g. tool 8's `next_step.sources`) |
| `topic_ids` / `brand_filter` / `market_filter` | no | AUTO mode topic selection |
| `source_scope` | no | `external` (default) / `owned` / `all` |
| `response_brand_mentioned` | no | AUTO filter before ranking |
| `top_n` | no | URLs to enrich (default 50, max 300) — **each is a crawl** |
| `brand_name` | no | override brand for owned/external |

**Returns:** `classified_sources` (with `source_content_brand_status`: own_only / own+comp / comp_only / no_brand / not_enriched), **`brand_citation_ranking`**, `matrix` (ownership × source_content_brand_status), `summary`.

> Distinguishes `response_brand_mentioned` (the LLM answer) from `source_content_brand_status` (the cited page). Per-page brand counts are de-duplicated (max per page), never multiplied by the number of citing reports.

---

## Configuration

### Owned domains (`owned_domains.json`)
Drives the owned/external axis independently of crawling. The server **UNIONs** each brand's list with `_default`, so a shared corporate domain applies to **every topic**. Subdomains match automatically (`accor.com` ⇒ `all.accor.com`, `ibis.accor.com`, …).

```json
{
  "_default": ["accor.com"],
  "IBIS":     ["accor.com", "ibis.com"],
  "Sofitel":  ["accor.com", "sofitel.com"]
}
```

To add a brand: edit the file and restart. Point to it with `OWNED_DOMAINS_PATH` if it is not next to the server file.

### Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `MINT_API_KEY` | yes | — | Mint.ai API key |
| `MINT_BASE_URL` | no | `https://api.getmint.ai/api` | Base URL |
| `HTTP_TIMEOUT` | no | `30` | Per-request read timeout (s) |
| `HTTP_MAX_CONCURRENT` | no | `8` | Max concurrent requests |
| `HTTP_MIN_INTERVAL` | no | `0.15` | **Min delay between request starts (s)** — throttle to avoid overloading the API; `0` disables |
| `TOOL_TIMEOUT` | no | `120` | Hard ceiling per tool call (s) |
| `OWNED_DOMAINS_PATH` | no | `./owned_domains.json` | Owned-domains mapping |

---

## Install & run

```bash
pip install -r requirements.txt          # or: pip install "httpx[socks]" socksio mcp starlette
export MINT_API_KEY="mint_live_..."
uvicorn mcp_mint_server:app --host 0.0.0.0 --port 8000 --workers 1
```

Endpoints: `GET /health`, `GET /sse` (Claude.ai / web clients), `POST /sse` + `POST /messages` (JSON-RPC).

### Claude Desktop
```json
{
  "mcpServers": {
    "mint-visibility": {
      "command": "python",
      "args": ["-m", "uvicorn", "mcp_mint_server:app", "--port", "8765"],
      "cwd": "/absolute/path/to/mint-mcp",
      "env": { "MINT_API_KEY": "mint_live_..." }
    }
  }
}
```

### Render
Build `pip install -r requirements.txt` · Start `uvicorn mcp_mint_server:app --host 0.0.0.0 --port $PORT --workers 1` · set `MINT_API_KEY` · connect Claude.ai to `https://<app>.onrender.com/sse`.

---

## Testing

- **Notebook:** `test_mint_tools.ipynb` — runs all 9 tools cell by cell with pandas tables. Launch Jupyter in this folder, paste your key in the Setup cell, run.
- **CLI:** `python test_live.py [domainId topicId]` — PASS/FAIL per tool.

Both need network access to `api.getmint.ai`.

---

## Changelog

### v5.2.0 — Owned-domain fix + throttling
- **FIX**: owned/external now UNIONs brand patterns with `_default`, so corporate domains (`accor.com` → `all.accor.com`, `ibis.accor.com`) are owned on every topic. Ships `owned_domains.json`.
- **PERF**: `HTTP_MIN_INTERVAL` throttle (default 0.15 s) spaces request starts.
- **CLEAN**: `mint_get_topic_overview` no longer returns `domain_source_analysis` (Mint reports it as 0%).

### v5.1.0 — Macro topic overview
- **NEW**: `mint_get_topic_overview` — one-call macro snapshot (score, share of voice, rank, model breakdown, competitors, top mentions). Surfaces `shareOfVoice` / `topMentions`. Tool count 8 → 9.

### v5.0.0 — Split source analysis
- Split `mint_get_raw_responses` into `mint_get_response_sources` (fast) + `mint_enrich_cited_sources` (deep). Citation-weighted metrics, per-report `topicId` fix, page counts no longer multiplied. Old tools kept as unlisted aliases.

### v4.2.0 — Slim catalog + per-topic models
- `mint_get_models_by_topic`; slim domains in the catalog; GLOBAL-by-default.

### v4.1.0 — Hardened
- Persistent HTTP client, input validation, typed errors + retry, tool annotations, SSE preserved.
