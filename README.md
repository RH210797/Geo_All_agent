# Mint.ai Visibility MCP Server

**v5.7.0** — MCP server exposing Mint.ai brand-visibility & competition data to any
MCP-compatible client (Claude Desktop, Claude.ai via SSE, custom agents).

It wraps the Mint public API (`https://api.getmint.ai/api`) behind a small set of
LLM-friendly tools: catalog discovery, scope resolution, visibility scores,
cited-source analysis, deep page enrichment, raw prompt-by-prompt export, and
head-to-head competition.

Every tool description carries a `USE FOR` / `DON'T USE FOR` block, and the catalog
tool ships a full **ROUTING MAP**, so an LLM can pick the right tool **without
guessing**. See [Which tool for which question](#which-tool-for-which-question).

---

## Quick start

```bash
pip install mcp httpx starlette uvicorn
export MINT_API_KEY="mint_live_xxx"        # REQUIRED
uvicorn mint_mcp_server:app --host 0.0.0.0 --port 8000
```

Transport is **SSE** (`GET /sse` to open the stream, `POST /messages` for JSON-RPC),
compatible with Render / Koyeb / Docker. Health check: `GET /` or `GET /health`
(returns `{"status":"ok","version":"5.7.0","tools":12}`).

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MINT_API_KEY` | — | **Required.** Mint API key sent as `X-API-Key`. |
| `MINT_BASE_URL` | `https://api.getmint.ai/api` | API base URL. |
| `HTTP_TIMEOUT` | `30` | Read timeout (s) per request. |
| `HTTP_MAX_CONCURRENT` | `8` | Max concurrent API requests. |
| `HTTP_MIN_INTERVAL` | `0.15` | Min delay (s) between request starts (throttle). |
| `TOOL_TIMEOUT` | `120` | Hard ceiling per tool call. Raise it for large `crawl_all` runs. |
| `OWNED_DOMAINS_PATH` | `./owned_domains.json` | Brand → owned-domains map for owned/external classification. |

`owned_domains.json` should ship a `"_default"` entry (e.g. `accor.com`, `ibis.com`)
so corporate domains are treated as owned on every topic.

---

## Which tool for which question

The LLM should route from the user's intent directly to one tool — no deliberation.
Always resolve IDs first (`mint_get_domains_and_topics`, or `mint_resolve_scope` when
the brand/market is fuzzy), then pick the tool below.

| The user asks… (FR / EN) | Tool |
|---|---|
| « Quelles marques / marchés j'ai ? » · *list my brands/topics* | `mint_get_domains_and_topics` |
| « IBIS France » (marché ambigu) · *which market do you mean* | `mint_resolve_scope` |
| « Quels modèles IA sont suivis ? » · *which models* | `mint_get_models_by_topic` |
| « Vue d'ensemble / dashboard du marché » · *overview, share of voice, rank* | `mint_get_topic_overview` |
| « Score jour par jour vs concurrents » · « courbe / évolution du score » · *daily scores, trend, chart* | `mint_get_topic_scores` |
| « Compare mes marchés / marques » · *compare markets in a table* | `mint_get_scores_overview` |
| « Quels sites / URLs sont les plus cités ? » · *top cited domains/URLs* | `mint_get_topic_sources` |
| « Sources propriétaires vs externes » · « quand je ne suis pas cité, qui apparaît ? » | `mint_get_response_sources` |
| « Les pages citées parlent-elles de ma marque ? » · « qui cite ma marque le plus ? » | `mint_enrich_cited_sources` |
| « Donne-moi TOUS les prompts + les sources utilisées » · « export brut / prompt par prompt / CSV » | `mint_get_raw_prompts` |
| « Qui gagne entre X et ses concurrents ? » · « mes forces / faiblesses » · *win rate* | `mint_get_competition_overview` |
| « Montre des exemples où je gagne / je perds » · *the actual comparison answers* | `mint_get_competition_responses` |

**Rule of thumb:** `best / win / vs / beat` → **competition** · `seen / score / share
of voice / rank` → **visibility** · `cited / source / URL / page` → **sources** · `raw
/ export / every prompt / dump` → **`mint_get_raw_prompts`**.

**Golden rule:** omit `startDate` / `endDate` / `models` unless the user explicitly
gave them. Default = last 30–90 days, all models combined (GLOBAL).

---

## Tools (12 exposed)

Always call **`mint_get_domains_and_topics` first** — every other tool needs a
`domainId` and/or `topicId`, and that tool returns them (plus the full ROUTING MAP).

### Catalog & scope

| Tool | What it does |
|---|---|
| `mint_get_domains_and_topics` | Lists every brand (domain) + market (topic) with their IDs. Start here. |
| `mint_resolve_scope` | Turns a fuzzy brand/market hint (`"IBIS"`) into a concrete `domainId`+`topicId`; returns a clarification QCM when it's ambiguous. |
| `mint_get_models_by_topic` | The AI models tracked for one topic. |
| `mint_get_topic_overview` | One-call MACRO snapshot: score (+variation), share of voice, brand rank, per-model breakdown, competitors, top mentions. |

### Visibility scores

| Tool | What it does |
|---|---|
| `mint_get_topic_scores` | Daily Brand-vs-Competitors scores, one topic, per model. The returned `dataset` (one row per Date × Entity × Model) is chart-ready — use it for **trend/curve** requests too. |
| `mint_get_scores_overview` | Average score per topic across many markets/brands (Markdown table + JSON rows). |

### Cited sources

| Tool | What it does |
|---|---|
| `mint_get_topic_sources` | Top cited domains/URLs for a topic, per model, over time (fast, no crawl). |
| `mint_get_response_sources` | Citation-weighted cited-source overview, owned vs external, brand-mentioned or not (fast, no crawl). Returns `next_step.sources` ready for enrichment. |
| `mint_enrich_cited_sources` | **DEEP** page enrichment: which cited *pages* mention your brand / competitors, with category. See [below](#mint_enrich_cited_sources--deep-enrichment). |

### Raw export

| Tool | What it does |
|---|---|
| `mint_get_raw_prompts` | **RAW**, no aggregation: exhaustive dump flattened to **one row per cited source** — prompt, source, topic, brand-mentioned, top-of-mind, inline citation. Flat `table` + `markdown_table` (CSV/Excel-ready) plus a `results` view grouped per answer. See [below](#mint_get_raw_prompts--raw-export). |

### Competition

| Tool | What it does |
|---|---|
| `mint_get_competition_overview` | MACRO head-to-head: win/loss/tie + win %, split by competitor and by model, plus brand strengths & weaknesses. |
| `mint_get_competition_responses` | DETAIL: the actual comparison prompts + LLM answers (winner, reasoning, strengths, weaknesses), paginated. For showing examples. |

> Backward-compat (registered but not listed): `mint_get_raw_responses`,
> `mint_enrich_sources`, plus the v4 unprefixed aliases.
> **Removed in v5.7.0:** `mint_get_visibility_trend` (use `mint_get_topic_scores`),
> `mint_refine_query` (the LLM narrows by asking the user directly).

---

## `mint_get_raw_prompts` — raw export

Answers *"give me every prompt sent to the platform and the sources the LLM used"* —
the un-aggregated data the source tools deliberately collapse into counts. No page
crawl. Two shapes in one call: a flat **`table`** (one row per cited source) and a
**`results`** view grouped per LLM answer.

### Two shapes

- **`table` / `markdown_table`** — one row **per cited source**: `topic | prompt |
  source | inline_citation | ownership | brand_mentioned | top_of_mind`. The prompt,
  topic, brand and top-of-mind repeat on each source row. Ready to export to CSV/Excel.
- **`results`** — grouped per LLM answer, with the full `citations[]` array and the
  response text for detail.

### Key parameters

| Param | Default | Notes |
|---|---|---|
| `response_brand_mentioned` | `all` | `true` / `false` / `all`. `false` = "who shows up when I'm NOT mentioned". |
| `include_response` | `true` | Include the LLM answer text in `results`. |
| `truncate_response` | `0` | Cap each answer's length (0 = full text). |
| `include_raw` | `false` | Add the untouched API object per response under `_raw` (to discover field names). |
| `page` / `limit` | `1` / `100` | Paginate over responses; each is exploded into its source rows. |

### Output

```jsonc
{
  "table": [                         // one row per cited source
    { "topic": "IBIS France",
      "prompt": "Meilleurs hôtels éco près de Gare de Lyon ?",
      "source": "booking.com",
      "inline_citation": "https://www.booking.com/hotel/fr/ibis-...",
      "ownership": "external",
      "brand_mentioned": true,
      "top_of_mind": "IBIS, B&B Hotels, Campanile" }
  ],
  "markdown_table": "| Topic | Prompt | Source | Inline citation | Owned? | Brand cité | Top of mind |\n...",
  "results": [                       // grouped per LLM answer (full detail)
    { "prompt": "...", "model": "gpt-5.1", "brand_mentioned": true,
      "top_of_mind": ["IBIS", "B&B Hotels"],
      "citations": [ { "url": "...", "website": "...", "ownership": "owned" } ],
      "response": "..." }
  ],
  "pagination": { "page": 1, "limit": 100, "total": 0, "table_rows": 0, "totalPages": 0 },
  "metadata": {
    "brand_name": "IBIS", "unique_prompts": 0,
    "note": "RAW export, no aggregation..."
  }
}
```

> If `prompt` (or `citations`) comes back `null`, call once with `include_raw=true`
> and inspect `_raw` to find the real API field name, then add it to `_PROMPT_KEYS`
> in the server. The `visibility/raw-results` payload may name the prompt differently
> from the competition endpoint.

---

## `mint_enrich_cited_sources` — deep enrichment

Answers *"do the cited pages actually mention my brand / competitors?"* and ranks
external sources by how much each cites your brand. Brand/competitor detection comes
from Mint's **stored page crawl**, looked up per `(reportId, url)` couple — the
endpoint reads stored crawls, it does **not** crawl on demand.

### Two modes

- **AUTO** (default): give `domainId` + topic selection. The tool fetches raw
  responses, ranks cited URLs by citation weight, and enriches them.
- **EXPLICIT**: pass `sources = [{url, reportId, topicId?}]` to enrich an exact set
  (e.g. the `next_step.sources` from `mint_get_response_sources`).

### Key parameters

| Param | Default | Notes |
|---|---|---|
| `top_n` | `50` | AUTO: number of most-cited URLs to enrich (max 300). |
| `crawl_all` | `false` | AUTO: enrich **all** in-scope URLs, ignoring `top_n` (batched by 100). Heavy — may exceed `TOOL_TIMEOUT`. |
| `max_reports_per_url` | `3` | How many reports to try per URL to find a stored crawl (stops at the first hit). `1` = strict, zero-redundancy. |
| `source_scope` | `external` | `external` / `owned` / `all`. |
| `startDate` / `endDate` | — | Scope to a period (e.g. one month). |

### Optimisation

Brand data is stored per `(reportId, url)`, but a page's content is identical no
matter which report cites it. The tool uses a **greedy cover**: each unique URL is
enriched only until the first report that returns crawl data, then skipped in all
others. This collapses the old per-couple redundancy (e.g. ~3,000 lookups for 100
URLs) into a few hundred, with the same coverage. URLs still missing a crawl fall
back to the next report that cites them, up to `max_reports_per_url`.

### Output

```jsonc
{
  "table": [                         // one row per URL
    { "url": "...", "brand": "IBIS",
      "brand_mentioned": "IBIS (8)",
      "competitors_cited": "Campanile (4), Hilton (3)",
      "category": "Travel & Tourism > Accommodations > ..." }
  ],
  "markdown_table": "| URL | Brand mentioned | Competitors cited | Category |\n...",
  "classified_sources": [...],
  "brand_citation_ranking": [...],   // external sources sorted by brand mentions
  "summary": { "own_only": .., "own+comp": .., "comp_only": .., "no_brand": .., "not_enriched": .. },
  "metadata": {
    "crawled_urls": 0, "category_only_urls": 0,   // real coverage
    "enrichment_lookups": 0, "max_reports_per_url": 3,
    "diagnostic": null                            // set when 0 brand found, explains why
  }
}
```

> If a run returns 0 brands, check `metadata.diagnostic` and `summary`: the
> most-cited URLs are often brandless infrastructure pages (map tiles, booking
> parking). Raise `top_n` or use `crawl_all`.

---

## Competition tools

### `mint_get_competition_overview` (macro)

`GET /domains/{domainId}/topics/{topicId}/competition/aggregated`

```jsonc
// args: { domainId, topicId, startDate?, endDate?, models?, competitors? }
{
  "brand": "IBIS", "topicName": "IBIS FR", "reportId": "...",
  "win_rate": { "wins": 0, "losses": 0, "ties": 0, "total": 0, "win_percentage": 0 },
  "by_competitor": { ... },
  "by_model": { ... },
  "strengths":  { "topCategories": [...], "totalMentions": 0, "categories": [...] },
  "weaknesses": { "topCategories": [...], "totalMentions": 0, "categories": [...] },
  "metadata": { "totalComparisons": 0, "competitorsAnalyzed": [...], "modelsIncluded": [...], "dateRange": {...} }
}
```

Use for *"Qui est le meilleur entre IBIS FR et ses concurrents ?"*. The aggregated
endpoint defaults to the **last 6 months** when no dates are given.

### `mint_get_competition_responses` (detail)

`GET /domains/{domainId}/topics/{topicId}/competition/raw-results`

```jsonc
// args: { domainId, topicId, winner_filter?, page?, limit?, models?, promptId?, truncate_response?, startDate?, endDate? }
{
  "results": [
    { "prompt": "...", "response": "...", "model": "gpt-5",
      "brand": "IBIS", "competitor": "B&B Hotels",
      "winner": "brand", "winner_reasoning": "...",
      "strengths": [...], "weaknesses": [...], "reportId": "..." }
  ],
  "pagination": { "page": 1, "limit": 10, "total": 0, "totalPages": 0 },
  "winner_counts_this_page": { "brand": 0, "competitor": 0, "tie": 0 }
}
```

`winner` is `brand` (your brand won), `competitor` (the rival won) or `tie`.
`winner_filter` is applied to the **current page** (the API paginates before
filtering). `truncate_response` caps each answer's length to keep payloads small.

---

## Changelog

### v5.7.0
- **REMOVED** `mint_refine_query` (guided-narrowing meta-tool — the LLM narrows by
  asking the user directly; `mint_resolve_scope` already disambiguates).
- **REMOVED** `mint_get_visibility_trend` (redundant — `mint_get_scores_overview`
  covers multi-topic comparison, `mint_get_topic_scores` returns a chart-ready
  day-by-day dataset).
- **DOCS** Tool descriptions, ROUTING MAP and this README refreshed for the 12-tool
  set; added a *Which tool for which question* decision table.

### v5.6.0
- **NEW** `mint_get_raw_prompts` — exhaustive raw-data export, one row per cited
  source (prompt · source · topic · brand-mentioned · top-of-mind · inline citation);
  flat `table` + `markdown_table` (CSV/Excel-ready) + grouped `results`.

### v5.4.0 – v5.5.0
- **NEW** `mint_resolve_scope` — fuzzy brand/market → IDs with QCM clarification.

### v5.3.0
- **NEW** `mint_get_competition_overview` and `mint_get_competition_responses`
  (head-to-head win rate + detailed prompts/answers).
- **PERF** `mint_enrich_cited_sources` greedy "one crawl hit per URL" cover; new
  `max_reports_per_url`.
- **NEW** `crawl_all` (enrich everything in scope); coverage metrics
  (`crawled_urls` / `category_only_urls` / `enrichment_lookups`); flat
  `table` + `markdown_table`; `metadata.diagnostic` on 0-brand results.

### v5.2.0
- Owned/external classification unions each brand's patterns with `_default`.
- `HTTP_MIN_INTERVAL` request throttle.

### v5.1.0
- `mint_get_topic_overview` one-call macro snapshot.

### v5.0.0
- Split the monolithic `mint_get_raw_responses` into `mint_get_response_sources`
  (fast) + `mint_enrich_cited_sources` (deep). Citation-weighted counts.
