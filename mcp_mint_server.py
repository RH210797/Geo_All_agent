"""
Mint.ai Visibility MCP Server — v4.1.0 (Hardened & Optimized)

MCP server exposing Mint.ai brand-visibility data to any MCP-compatible client
(Claude Desktop, Claude.ai via SSE, custom agents).

═══════════════════════════════════════════════════════════════════
WHAT CHANGED IN v4.1.0 (from v4.0.0)
═══════════════════════════════════════════════════════════════════
PERF   — Persistent httpx.AsyncClient (connection pooling, TLS reuse)
         instead of creating a new client per request.
PERF   — Startup/shutdown lifecycle via Starlette on_startup/on_shutdown.
ROBUST — Input validation helpers: all tool arguments are checked early
         with clear, actionable error messages returned to the LLM.
ROBUST — Tool names prefixed with "mint_" to avoid collisions when
         cohabiting with other MCP servers. Backward-compat aliases
         kept in the dispatcher.
ROBUST — Tool annotations (readOnlyHint, openWorldHint, etc.) declared
         inside inputSchema metadata so clients know every tool is safe.
DOCS   — Tool descriptions rewritten in English for broader audience,
         with USE FOR / DON'T USE FOR sections and concrete examples.
FIX    — owned_domains.json path resolution is now robust when running
         via uvicorn from a different cwd.
CLEAN  — Consistent English logging (production-friendly).
COMPAT — SSE transport preserved (Render + Claude.ai compatible).
         No breaking change on wire protocol or deploy process.
═══════════════════════════════════════════════════════════════════

Tools (7):
  mint_get_domains_and_topics  — catalog discovery
  mint_get_topic_scores        — Brand vs Competitors, 1 topic, per-model
  mint_get_scores_overview     — multi-topic summary table
  mint_get_visibility_trend    — time series for charts
  mint_get_topic_sources       — top cited domains/URLs, 1 topic
  mint_get_raw_responses       — core v4: 2-axis source classification
  mint_enrich_sources          — direct batch URL enrichment

Environment variables:
  MINT_API_KEY         — API key (REQUIRED)
  MINT_BASE_URL        — Base URL (default: https://api.getmint.ai/api)
  HTTP_TIMEOUT         — Timeout in seconds (default: 30)
  HTTP_MAX_CONCURRENT  — Max concurrent API requests (default: 8)
  OWNED_DOMAINS_PATH   — Path to owned_domains.json (default: ./owned_domains.json)
"""

import asyncio
import json
import logging
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool

from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


# ══════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════

MINT_API_KEY: str = os.getenv("MINT_API_KEY", "")
MINT_BASE_URL: str = os.getenv("MINT_BASE_URL", "https://api.getmint.ai/api")
HTTP_TIMEOUT: float = float(os.getenv("HTTP_TIMEOUT", "30.0"))
HTTP_MAX_CONCURRENT: int = int(os.getenv("HTTP_MAX_CONCURRENT", "8"))

_OWNED_DEFAULT_PATH = Path(__file__).resolve().parent / "owned_domains.json"
OWNED_DOMAINS_PATH: str = os.getenv("OWNED_DOMAINS_PATH", str(_OWNED_DEFAULT_PATH))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("mint_mcp")

if not MINT_API_KEY:
    logger.warning("MINT_API_KEY is not set — all API calls will fail")

_HTTP_SEMAPHORE = asyncio.Semaphore(HTTP_MAX_CONCURRENT)

__version__ = "4.1.0"

server = Server("mint_visibility_mcp")


# ══════════════════════════════════════════════════════════════════
# PERSISTENT HTTP CLIENT (connection pooling + TLS reuse)
# ══════════════════════════════════════════════════════════════════

_http_client: httpx.AsyncClient | None = None


async def _start_http_client() -> None:
    """Create persistent HTTP client on server startup."""
    global _http_client
    _http_client = httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        limits=httpx.Limits(
            max_connections=HTTP_MAX_CONCURRENT + 2,
            max_keepalive_connections=HTTP_MAX_CONCURRENT,
        ),
    )
    logger.info("HTTP client started (timeout=%.0fs, pool=%d)", HTTP_TIMEOUT, HTTP_MAX_CONCURRENT)


async def _stop_http_client() -> None:
    """Close persistent HTTP client on server shutdown."""
    global _http_client
    if _http_client:
        await _http_client.aclose()
        _http_client = None
        logger.info("HTTP client closed")


# ══════════════════════════════════════════════════════════════════
# OWNED DOMAINS MAPPING
# ══════════════════════════════════════════════════════════════════

def _load_owned_domains_map() -> dict:
    """Load brand -> owned-domains mapping. Silent fallback if missing."""
    path = Path(OWNED_DOMAINS_PATH)
    if not path.exists():
        logger.warning("owned_domains.json not found at %s — all URLs classified as 'external'", path)
        return {"_default": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.error("owned_domains.json is invalid (%s) — falling back to empty map", exc)
        return {"_default": []}
    cleaned = {k: v for k, v in raw.items() if not k.startswith("_") or k == "_default"}
    logger.info("owned_domains.json loaded — %d brand(s) + _default", len([k for k in cleaned if k != "_default"]))
    return cleaned


OWNED_DOMAINS_MAP: dict = _load_owned_domains_map()


def get_owned_patterns(brand_name: str) -> list:
    """Return owned domain patterns for a brand, with _default fallback."""
    return OWNED_DOMAINS_MAP.get(brand_name, OWNED_DOMAINS_MAP.get("_default", []))


# ══════════════════════════════════════════════════════════════════
# URL & DATE HELPERS
# ══════════════════════════════════════════════════════════════════

def domain_from_url(url: str) -> str | None:
    """Extract domain from URL, stripping 'www.'. Returns None if invalid."""
    if not url:
        return None
    try:
        netloc = urlparse(url).netloc.lower()
        if not netloc:
            return None
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return None


def is_owned_domain(url: str, owned_patterns: list) -> bool:
    """True if URL belongs to an owned domain (exact match or subdomain)."""
    d = domain_from_url(url)
    if not d:
        return False
    return any(d == p or d.endswith("." + p) for p in owned_patterns)


def default_date_range(days: int) -> tuple[str, str]:
    """Return (startDate, endDate) as YYYY-MM-DD strings."""
    end = date.today()
    start = end - timedelta(days=days)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def week_start(iso_date: str) -> str:
    """Return the Monday of the week containing iso_date."""
    d = datetime.fromisoformat(iso_date[:10]).date()
    return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")


def month_start(iso_date: str) -> str:
    """Return the first day of the month of iso_date."""
    return datetime.fromisoformat(iso_date[:10]).date().strftime("%Y-%m-01")


def day_iso(iso_date: str) -> str:
    return iso_date[:10]


BIN_FUNCTIONS = {"day": day_iso, "week": week_start, "month": month_start}


# ══════════════════════════════════════════════════════════════════
# INPUT VALIDATION HELPERS
# ══════════════════════════════════════════════════════════════════

class InvalidInput(Exception):
    """Raised when tool arguments fail validation."""


def require_str(args: dict, key: str, label: str | None = None) -> str:
    """Extract a required string argument or raise InvalidInput."""
    val = args.get(key)
    if not val or not isinstance(val, str) or not val.strip():
        raise InvalidInput(f"'{label or key}' is required and must be a non-empty string.")
    return val.strip()


def optional_str(args: dict, key: str) -> str | None:
    val = args.get(key)
    if val is None:
        return None
    if not isinstance(val, str):
        raise InvalidInput(f"'{key}' must be a string, got {type(val).__name__}.")
    return val.strip() or None


def optional_int(args: dict, key: str, default: int, min_val: int = 1, max_val: int = 10000) -> int:
    val = args.get(key, default)
    if val is None:
        return default
    try:
        val = int(val)
    except (TypeError, ValueError):
        raise InvalidInput(f"'{key}' must be an integer, got '{val}'.")
    if val < min_val or val > max_val:
        raise InvalidInput(f"'{key}' must be between {min_val} and {max_val}, got {val}.")
    return val


def optional_bool(args: dict, key: str, default: bool = False) -> bool:
    val = args.get(key, default)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes")
    return bool(val)


def optional_str_list(args: dict, key: str) -> list[str] | None:
    val = args.get(key)
    if val is None:
        return None
    if isinstance(val, list):
        return [str(v).strip() for v in val if v]
    if isinstance(val, str):
        return [val.strip()] if val.strip() else None
    raise InvalidInput(f"'{key}' must be a string or list of strings.")


def optional_enum(args: dict, key: str, allowed: set, default: str) -> str:
    val = args.get(key, default)
    if val is None:
        return default
    if not isinstance(val, str):
        raise InvalidInput(f"'{key}' must be a string.")
    val = val.strip()
    if val not in allowed:
        raise InvalidInput(f"'{key}' must be one of {sorted(allowed)}, got '{val}'.")
    return val


# ══════════════════════════════════════════════════════════════════
# HTTP CLIENT — TYPED ERRORS + EXPONENTIAL RETRY
# ══════════════════════════════════════════════════════════════════

class MintAPIError(Exception):
    """Generic Mint API error."""
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class AuthError(MintAPIError):
    """401 — invalid or missing API key."""


class NotFoundError(MintAPIError):
    """404 — resource not found."""


class RateLimitError(MintAPIError):
    """429 — rate limit exceeded."""


def _map_http_error(e: httpx.HTTPStatusError) -> MintAPIError:
    sc = e.response.status_code
    try:
        body = e.response.json()
        msg = body.get("message") or body.get("error") or str(body)
    except Exception:
        msg = e.response.text[:200]

    if sc == 401:
        return AuthError(f"Invalid or missing API key: {msg}", sc)
    if sc == 404:
        return NotFoundError(f"Resource not found: {msg}", sc)
    if sc == 429:
        return RateLimitError(f"Rate limit exceeded: {msg}", sc)
    return MintAPIError(f"HTTP {sc}: {msg}", sc)


async def _http_request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    max_retries: int = 3,
) -> Any:
    """HTTP request with semaphore, persistent client, and exponential retry on 429/5xx."""
    if not MINT_API_KEY:
        raise RuntimeError("MINT_API_KEY environment variable is required.")

    client = _http_client
    if client is None:
        # Fallback: create ephemeral client if lifecycle didn't run (e.g. tests)
        client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)

    url = f"{MINT_BASE_URL}{path}"
    backoff = 1.0

    for attempt in range(max_retries):
        async with _HTTP_SEMAPHORE:
            try:
                headers = {"X-API-Key": MINT_API_KEY}
                if json_body is not None:
                    headers["Content-Type"] = "application/json"
                r = await client.request(
                    method, url,
                    params=params, json=json_body,
                    headers=headers,
                )
                r.raise_for_status()
                return r.json()
            except httpx.HTTPStatusError as e:
                sc = e.response.status_code
                if sc in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                    reset = e.response.headers.get("X-RateLimit-Reset")
                    wait = float(reset) if reset else backoff
                    logger.warning(
                        "HTTP %d on %s — retrying in %.1fs (%d/%d)",
                        sc, path, wait, attempt + 1, max_retries,
                    )
                    await asyncio.sleep(wait)
                    backoff *= 2
                    continue
                raise _map_http_error(e) from e
            except httpx.RequestError as e:
                if attempt < max_retries - 1:
                    logger.warning("Network error on %s — retrying in %.1fs: %s", path, backoff, e)
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise MintAPIError(f"Network error: {e}") from e

    raise MintAPIError("Max retries exceeded")


async def fetch_get(path: str, params: dict | None = None) -> Any:
    return await _http_request("GET", path, params=params or {})


async def fetch_post(path: str, body: dict) -> Any:
    return await _http_request("POST", path, json_body=body)


# ══════════════════════════════════════════════════════════════════
# CATALOG HELPERS
# ══════════════════════════════════════════════════════════════════

def filter_topics(
    all_topics: list,
    topic_ids: list | None = None,
    brand_filter: str | None = None,
    market_filter: str | None = None,
) -> list:
    """Filter topics by IDs, brand name, and/or market keyword."""
    topics = all_topics
    if topic_ids:
        wanted = set(topic_ids)
        topics = [t for t in topics if t["topicId"] in wanted]
    if brand_filter:
        bf = brand_filter.upper()
        topics = [t for t in topics if bf in (t["domainName"] or "").upper()]
    if market_filter:
        mf = market_filter.upper()
        topics = [t for t in topics if mf in (t["topicName"] or "").upper()]
    return topics


# ══════════════════════════════════════════════════════════════════
# TOOL 1/7 — mint_get_domains_and_topics
# ══════════════════════════════════════════════════════════════════

async def _tool_get_domains_and_topics(_args: dict) -> dict:
    """Fetch all domains (brands) and their topics (markets)."""
    domains = await fetch_get("/domains")
    topics, mapping, errors = [], {}, []

    for d in domains:
        d_id = d.get("id")
        d_name = d.get("displayName") or d.get("name") or "Unknown"
        try:
            d_topics = await fetch_get(f"/domains/{d_id}/topics")
        except Exception as e:
            logger.warning("Failed to fetch topics for %s: %s", d_name, e)
            errors.append({"domainId": d_id, "domainName": d_name, "error": str(e)[:200]})
            continue

        for t in d_topics:
            t_id = t.get("id")
            t_name = t.get("displayName") or t.get("name") or "Unknown"
            topics.append({
                "domainId": d_id, "domainName": d_name,
                "topicId": t_id, "topicName": t_name,
            })
            mapping[f"{d_name} > {t_name}"] = {"domainId": d_id, "topicId": t_id}

    return {"domains": domains, "topics": topics, "mapping": mapping, "errors": errors}


# ══════════════════════════════════════════════════════════════════
# TOOL 2/7 — mint_get_topic_scores
# ══════════════════════════════════════════════════════════════════

async def _tool_get_topic_scores(args: dict) -> dict:
    """Detailed Brand vs Competitors scores for ONE topic, per AI model."""
    domain_id = require_str(args, "domainId")
    topic_id = require_str(args, "topicId")
    start_date = optional_str(args, "startDate")
    end_date = optional_str(args, "endDate")
    models = optional_str(args, "models")

    if not start_date or not end_date:
        start_date, end_date = default_date_range(days=30)

    base_params = {
        "startDate": start_date, "endDate": end_date,
        "latestOnly": "false", "page": 1, "limit": 1000,
    }
    endpoint = f"/domains/{domain_id}/topics/{topic_id}/visibility/aggregated"

    global_data = await fetch_get(endpoint, base_params)
    available = global_data.get("availableModels", [])

    if models:
        requested = {m.strip() for m in models.split(",")}
        available = [m for m in available if m in requested]

    async def fetch_model(m):
        try:
            return m, await fetch_get(endpoint, {**base_params, "models": m})
        except Exception as e:
            logger.warning("Model %s fetch failed: %s", m, e)
            return m, None

    results = await asyncio.gather(*[fetch_model(m) for m in available])
    by_model = {m: d for m, d in results if d is not None}

    dataset = []

    def add_rows(data, model_name):
        for entry in data.get("chartData", []):
            dt = entry.get("date")
            dataset.append({
                "Date": dt, "EntityName": "Brand", "EntityType": "Brand",
                "Score": entry.get("brand"), "Model": model_name,
            })
            for c_name, c_score in (entry.get("competitors") or {}).items():
                dataset.append({
                    "Date": dt, "EntityName": c_name, "EntityType": "Competitor",
                    "Score": c_score, "Model": model_name,
                })

    add_rows(global_data, "GLOBAL")
    for m, d in by_model.items():
        add_rows(d, m)

    return {
        "status": "success",
        "data": {
            "dataset": dataset,
            "metadata": {
                "models": ["GLOBAL"] + list(by_model.keys()),
                "startDate": start_date, "endDate": end_date,
                "topicId": topic_id, "domainId": domain_id,
            },
        },
    }


# ══════════════════════════════════════════════════════════════════
# TOOL 3/7 — mint_get_scores_overview
# ══════════════════════════════════════════════════════════════════

async def _tool_get_scores_overview(args: dict) -> dict:
    """Average visibility score for MULTIPLE topics in one call."""
    brand_filter = optional_str(args, "brand_filter")
    market_filter = optional_str(args, "market_filter")
    topic_ids = optional_str_list(args, "topic_ids")
    start_date = optional_str(args, "startDate")
    end_date = optional_str(args, "endDate")
    models = optional_str(args, "models")

    if not start_date or not end_date:
        start_date, end_date = default_date_range(days=90)

    catalog = await _tool_get_domains_and_topics({})
    topics = filter_topics(catalog["topics"], topic_ids, brand_filter, market_filter)

    if not topics:
        return {
            "status": "error",
            "message": f"No topics found (brand='{brand_filter}', market='{market_filter}', topic_ids={topic_ids}). "
                       "Use mint_get_domains_and_topics first to see available brands and markets.",
        }

    params: dict[str, Any] = {"limit": 100, "startDate": start_date, "endDate": end_date}
    if models:
        params["models"] = models

    async def fetch_one(t):
        try:
            d = await fetch_get(
                f"/domains/{t['domainId']}/topics/{t['topicId']}/visibility", params,
            )
            scores = [
                float(r["averageScore"])
                for r in d.get("reports", [])
                if r.get("averageScore") is not None
            ]
            avg = round(sum(scores) / len(scores), 1) if scores else None
            return {"brand": t["domainName"], "topic": t["topicName"],
                    "avg_score": avg, "data_points": len(scores), "error": None}
        except Exception as e:
            return {"brand": t["domainName"], "topic": t["topicName"],
                    "avg_score": None, "data_points": 0, "error": str(e)[:100]}

    rows = list(await asyncio.gather(*[fetch_one(t) for t in topics]))
    rows.sort(key=lambda r: (r["brand"], -(r["avg_score"] if r["avg_score"] is not None else -1)))

    def score_emoji(s):
        if s is None: return "⚠️"
        if s >= 60:   return "🟢"
        if s >= 40:   return "🟡"
        if s >= 20:   return "🟠"
        return "🔴"

    filter_info = ""
    if brand_filter:  filter_info += f" | brand: {brand_filter}"
    if market_filter: filter_info += f" | market: {market_filter}"
    if models:        filter_info += f" | models: {models}"

    lines = [
        f"## 📊 Average scores — {start_date} → {end_date}",
        f"*{len(rows)} topics{filter_info}*",
        "",
        "| Brand | Topic | Avg Score | Reports | Status |",
        "|-------|-------|:---------:|:-------:|--------|",
    ]
    prev = None
    for r in rows:
        brand_d = r["brand"] if r["brand"] != prev else ""
        prev = r["brand"]
        score = f"**{r['avg_score']}**" if r["avg_score"] is not None else "—"
        status = score_emoji(r["avg_score"]) if not r["error"] else f"❌ {r['error'][:30]}"
        lines.append(f"| {brand_d} | {r['topic']} | {score} | {r['data_points']} | {status} |")

    valid = [r["avg_score"] for r in rows if r["avg_score"] is not None]
    if valid:
        gavg = round(sum(valid) / len(valid), 1)
        best = max(rows, key=lambda r: r["avg_score"] or -1)
        worst = min(rows, key=lambda r: r["avg_score"] if r["avg_score"] is not None else 9999)
        lines += [
            "",
            "---",
            f"**Average:** {gavg} | **Best:** {best['topic']} ({best['avg_score']}) | **Lowest:** {worst['topic']} ({worst['avg_score']})",
            "_🟢 ≥60 | 🟡 40–59 | 🟠 20–39 | 🔴 <20 | ⚠️ no data_",
        ]

    return {
        "status": "success",
        "markdown_table": "\n".join(lines),
        "rows": rows,
        "metadata": {
            "startDate": start_date, "endDate": end_date,
            "models": models or "all (cross-model)",
            "topic_count": len(rows),
        },
    }


# ══════════════════════════════════════════════════════════════════
# TOOL 4/7 — mint_get_visibility_trend
# ══════════════════════════════════════════════════════════════════

async def _tool_get_visibility_trend(args: dict) -> dict:
    """Binned time series of visibility scores (day/week/month) for charts."""
    brand_filter = optional_str(args, "brand_filter")
    market_filter = optional_str(args, "market_filter")
    topic_ids = optional_str_list(args, "topic_ids")
    start_date = optional_str(args, "startDate")
    end_date = optional_str(args, "endDate")
    models = optional_str(args, "models")
    granularity = optional_enum(args, "granularity", {"day", "week", "month"}, "week")
    aggregation = optional_enum(args, "aggregation", {"average", "per_topic"}, "average")

    if not start_date or not end_date:
        end_date = date.today().strftime("%Y-%m-%d")
        start_date = date(date.today().year, 1, 1).strftime("%Y-%m-%d")

    catalog = await _tool_get_domains_and_topics({})
    topics = filter_topics(catalog["topics"], topic_ids, brand_filter, market_filter)

    if not topics:
        return {
            "status": "error",
            "message": f"No topics found (brand='{brand_filter}', market='{market_filter}'). "
                       "Use mint_get_domains_and_topics to list available brands and markets.",
        }

    params: dict[str, Any] = {"limit": 1000, "startDate": start_date, "endDate": end_date}
    if models:
        params["models"] = models

    async def fetch_topic_points(t):
        try:
            d = await fetch_get(
                f"/domains/{t['domainId']}/topics/{t['topicId']}/visibility", params,
            )
            pts = []
            for r in d.get("reports", []):
                s = r.get("averageScore")
                when = r.get("date") or r.get("createdAt") or r.get("reportDate")
                if s is not None and when:
                    pts.append({"date": when[:10], "score": float(s)})
            return t, pts
        except Exception as e:
            logger.warning("Trend fetch failed for topic %s: %s", t.get("topicName"), e)
            return t, []

    results = await asyncio.gather(*[fetch_topic_points(t) for t in topics])
    bin_fn = BIN_FUNCTIONS[granularity]

    if aggregation == "per_topic":
        series = []
        for t, pts in results:
            if not pts:
                continue
            buckets: dict[str, list] = defaultdict(list)
            for p in pts:
                buckets[bin_fn(p["date"])].append(p["score"])
            points = [
                {"date": k, "score": round(sum(v) / len(v), 1), "n": len(v)}
                for k, v in sorted(buckets.items())
            ]
            series.append({"name": f"{t['domainName']} > {t['topicName']}", "points": points})
    else:
        buckets_avg: dict[str, list] = defaultdict(list)
        for _, pts in results:
            for p in pts:
                buckets_avg[bin_fn(p["date"])].append(p["score"])
        points = [
            {"date": k, "score": round(sum(v) / len(v), 1), "n": len(v)}
            for k, v in sorted(buckets_avg.items())
        ]
        label = f"{brand_filter or 'All'}{' / ' + market_filter if market_filter else ''} ({granularity} avg)"
        series = [{"name": label, "points": points}]

    return {
        "status": "success",
        "series": series,
        "metadata": {
            "granularity": granularity, "aggregation": aggregation,
            "startDate": start_date, "endDate": end_date,
            "topics_n": len(topics), "models": models or "all",
        },
        "chart_hint": "line",
    }


# ══════════════════════════════════════════════════════════════════
# TOOL 5/7 — mint_get_topic_sources
# ══════════════════════════════════════════════════════════════════

async def _tool_get_topic_sources(args: dict) -> dict:
    """Top cited domains and URLs for ONE topic, per AI model."""
    domain_id = require_str(args, "domainId")
    topic_id = require_str(args, "topicId")
    start_date = optional_str(args, "startDate")
    end_date = optional_str(args, "endDate")
    models = optional_str(args, "models")

    if not start_date or not end_date:
        start_date, end_date = default_date_range(days=90)

    base_params = {
        "startDate": start_date, "endDate": end_date,
        "includeDetailedResults": "true", "latestOnly": "false",
        "page": 1, "limit": 1000,
    }
    endpoint = f"/domains/{domain_id}/topics/{topic_id}/visibility/aggregated"

    global_data = await fetch_get(endpoint, base_params)
    available = global_data.get("availableModels", [])

    if models:
        requested = {m.strip() for m in models.split(",")}
        available = [m for m in available if m in requested]

    async def fetch_model(m):
        try:
            return m, await fetch_get(endpoint, {**base_params, "models": m})
        except Exception as e:
            logger.warning("Sources fetch failed for model %s: %s", m, e)
            return m, None

    results = await asyncio.gather(*[fetch_model(m) for m in available])
    by_model = {m: d for m, d in results if d is not None}

    top_domains: list[dict] = []
    top_urls: list[dict] = []
    domains_over_time: list[dict] = []
    urls_over_time: list[dict] = []
    metrics: list[dict] = []

    def extract(data, model_name):
        for i, it in enumerate(data.get("topDomains") or [], 1):
            top_domains.append({
                "Model": model_name,
                "Domain": it.get("domain") or it.get("linkDomain") or "",
                "CitationCount": it.get("count") or it.get("citationCount") or 0,
                "Rank": i,
            })
        for i, it in enumerate(data.get("topCitedUrls") or [], 1):
            top_urls.append({
                "Model": model_name,
                "Url": it.get("url") or it.get("link") or "",
                "Domain": it.get("domain") or it.get("linkDomain") or "",
                "CitationCount": it.get("count") or it.get("citationCount") or 0,
                "Rank": i,
            })
        for entry in data.get("topDomainsOverTime") or []:
            for dom, cnt in (entry.get("domains") or {}).items():
                domains_over_time.append({
                    "Model": model_name, "Date": entry.get("date") or "",
                    "Domain": dom, "Count": cnt,
                })
        for entry in data.get("topUrlsOverTime") or []:
            for u, cnt in (entry.get("urls") or {}).items():
                urls_over_time.append({
                    "Model": model_name, "Date": entry.get("date") or "",
                    "Url": u, "Count": cnt,
                })
        metrics.append({
            "Model": model_name,
            "TotalPrompts": data.get("totalPromptsTested") or 0,
            "TotalAnswers": data.get("totalAnswers") or 0,
            "TotalCitations": data.get("totalCitations") or 0,
            "ReportCount": data.get("reportCount") or 0,
        })

    extract(global_data, "GLOBAL")
    for m, d in by_model.items():
        extract(d, m)

    return {
        "status": "success",
        "data": {
            "top_domains": top_domains,
            "top_urls": top_urls,
            "domains_over_time": domains_over_time,
            "urls_over_time": urls_over_time,
            "global_metrics": metrics,
            "metadata": {
                "models": ["GLOBAL"] + list(by_model.keys()),
                "startDate": start_date, "endDate": end_date,
                "topicId": topic_id, "domainId": domain_id,
            },
        },
    }


# ══════════════════════════════════════════════════════════════════
# TOOL 6/7 — mint_get_raw_responses (CORE v4)
# ══════════════════════════════════════════════════════════════════
#
# 2 INDEPENDENT axes per URL:
#   Axis 1: ownership    = "owned" | "external"     (regex on domain)
#   Axis 2: brand_status = own_only | own+comp | comp_only | no_brand | not_enriched
#
# Strict (reportId, url) coupling respected for enrichment API.

async def _fetch_raw_one_topic(domain_id: str, topic_id: str, params: dict) -> list:
    """Paginate through all raw-results for one topic."""
    p = dict(params)
    p["page"] = 1
    out: list = []
    MAX_PAGES = 50
    while True:
        resp = await fetch_get(
            f"/domains/{domain_id}/topics/{topic_id}/visibility/raw-results", p,
        )
        out.extend(resp.get("results") or [])
        pg = resp.get("pagination") or {}
        if p["page"] >= pg.get("totalPages", 1):
            break
        p["page"] += 1
        if p["page"] > MAX_PAGES:
            logger.warning("Raw results pagination capped at %d pages", MAX_PAGES)
            break
    return out


async def _enrich_report_batch(
    domain_id: str, report_id: str, urls: list, topic_id: str | None = None,
) -> dict:
    """Enrich a batch of URLs for ONE reportId. Auto-chunks at 100 (API limit)."""
    result: dict = {}
    for i in range(0, len(urls), 100):
        chunk = urls[i:i + 100]
        body: dict[str, Any] = {"urls": chunk, "reportId": report_id}
        if topic_id:
            body["topicId"] = topic_id
        try:
            resp = await fetch_post(f"/domains/{domain_id}/sources/enrichment", body)
            result.update(resp)
        except Exception as e:
            logger.warning("Enrich chunk failed (reportId=%s, %d urls): %s", report_id, len(chunk), e)
    return result


def _classify_url(url: str, agg: dict, owned_patterns: list) -> tuple[str, str]:
    """Return (ownership, brand_status) for a URL."""
    ownership = "owned" if is_owned_domain(url, owned_patterns) else "external"

    if agg["couples_enriched"] == 0:
        brand_status = "not_enriched"
    elif agg["has_own"] and agg["has_comp"]:
        brand_status = "own+comp"
    elif agg["has_own"]:
        brand_status = "own_only"
    elif agg["has_comp"]:
        brand_status = "comp_only"
    else:
        brand_status = "no_brand"

    return ownership, brand_status


async def _tool_get_raw_responses(args: dict) -> dict:
    """Fine-grained source analysis with 2-axis classification."""
    domain_id = require_str(args, "domainId")
    topic_ids = optional_str_list(args, "topic_ids")
    brand_filter = optional_str(args, "brand_filter")
    market_filter = optional_str(args, "market_filter")
    start_date = optional_str(args, "startDate")
    end_date = optional_str(args, "endDate")
    models = optional_str(args, "models")
    latest_only = optional_bool(args, "latestOnly", False)
    response_brand_mentioned = optional_enum(
        args, "response_brand_mentioned", {"true", "false", "all"}, "all",
    )
    aggregate = optional_enum(args, "aggregate", {"classified", "sources", "none"}, "classified")
    ownership_filter = optional_enum(args, "ownership_filter", {"owned", "external", "all"}, "all")
    top_n = optional_int(args, "top_n", default=30, min_val=1, max_val=500)

    # brand_status_filter: string, list, or None
    valid_statuses = {"own_only", "own+comp", "comp_only", "no_brand", "not_enriched"}
    raw_bsf = args.get("brand_status_filter")
    if raw_bsf is None or raw_bsf == "all":
        wanted_statuses = valid_statuses
    elif isinstance(raw_bsf, str):
        if raw_bsf not in valid_statuses:
            raise InvalidInput(f"brand_status_filter '{raw_bsf}' invalid. Must be one of {sorted(valid_statuses)}.")
        wanted_statuses = {raw_bsf}
    elif isinstance(raw_bsf, list):
        wanted_statuses = set(raw_bsf)
        unknown = wanted_statuses - valid_statuses
        if unknown:
            raise InvalidInput(f"brand_status_filter values {unknown} invalid. Must be from {sorted(valid_statuses)}.")
    else:
        raise InvalidInput(f"brand_status_filter must be a string or list, got {type(raw_bsf).__name__}.")

    # Resolve topics
    catalog = await _tool_get_domains_and_topics({})
    topics = filter_topics(catalog["topics"], topic_ids, brand_filter, market_filter)
    topics = [t for t in topics if t["domainId"] == domain_id]
    if not topics:
        return {
            "status": "error",
            "message": f"No topics found for domainId={domain_id}. "
                       "Use mint_get_domains_and_topics to verify the domainId.",
        }

    brand_name = topics[0]["domainName"]
    owned_patterns = get_owned_patterns(brand_name)

    # Fetch raw results in parallel
    params: dict[str, Any] = {"limit": 100}
    if start_date:  params["startDate"] = start_date
    if end_date:    params["endDate"] = end_date
    if models:      params["models"] = models
    if latest_only: params["latestOnly"] = "true"

    async def fetch_topic(t):
        results = await _fetch_raw_one_topic(t["domainId"], t["topicId"], params)
        for r in results:
            r["_topicName"] = t["topicName"]
            r["_domainName"] = t["domainName"]
        return results

    fetched = await asyncio.gather(*[fetch_topic(t) for t in topics])
    all_raw = [r for batch in fetched for r in batch]

    # Filter at response level
    if response_brand_mentioned == "true":
        responses = [r for r in all_raw if r.get("brandMentioned") is True]
    elif response_brand_mentioned == "false":
        responses = [r for r in all_raw if r.get("brandMentioned") is False]
    else:
        responses = all_raw

    # ─── Mode "none": raw responses ───
    if aggregate == "none":
        return {
            "status": "success",
            "responses": responses,
            "metadata": {
                "topics_n": len(topics), "raw_total": len(all_raw),
                "after_response_filter": len(responses),
                "brand_name": brand_name, "owned_patterns": owned_patterns,
            },
        }

    # ─── Mode "sources": top domains/URLs without enrichment ───
    if aggregate == "sources":
        domain_c: Counter = Counter()
        url_c: Counter = Counter()
        tom_c: Counter = Counter()
        for r in responses:
            for d in (r.get("responseDomains") or []):
                if d.get("domain"):
                    domain_c[d["domain"]] += 1
            for c in (r.get("citations") or []):
                dom = c.get("website") or domain_from_url(c.get("url") or "")
                if dom:
                    domain_c[dom] += 1
                if c.get("url"):
                    url_c[c["url"]] += 1
            for b in (r.get("topOfMind") or []):
                tom_c[b] += 1

        return {
            "status": "success",
            "top_domains": [{"domain": d, "count": c} for d, c in domain_c.most_common(top_n)],
            "top_urls": [{"url": u, "count": c} for u, c in url_c.most_common(top_n)],
            "top_of_mind": [{"brand": b, "count": c} for b, c in tom_c.most_common(top_n)],
            "metadata": {
                "topics_n": len(topics), "raw_total": len(all_raw),
                "after_response_filter": len(responses), "brand_name": brand_name,
                "filters": {"response_brand_mentioned": response_brand_mentioned, "models": models},
            },
        }

    # ─── Mode "classified": enrichment + 2-axis classification ───

    # Step 1: collect (reportId, url) pairs WITHOUT cross-report dedup
    report_to_urls: dict[str, set] = defaultdict(set)
    for r in responses:
        rid = r.get("reportId")
        if not rid:
            continue
        for c in (r.get("citations") or []):
            u = c.get("url")
            if u:
                report_to_urls[rid].add(u)

    # Step 2: enrich per reportId in parallel
    async def one_report(rid):
        urls = list(report_to_urls[rid])
        if not urls:
            return {}
        data = await _enrich_report_batch(domain_id, rid, urls, topic_id=topics[0]["topicId"])
        return {(rid, url): payload for url, payload in data.items()}

    enrich_results = await asyncio.gather(*[one_report(rid) for rid in report_to_urls])
    enriched_by_couple: dict = {}
    for batch in enrich_results:
        enriched_by_couple.update(batch)

    # Step 3: aggregate per URL
    url_to_agg: dict = defaultdict(lambda: {
        "has_own": False, "has_comp": False,
        "own_brands": set(), "comp_brands": set(),
        "own_count_total": 0, "comp_count_total": 0,
        "categories": set(),
        "couples_enriched": 0, "couples_total": 0,
    })

    for rid, urls_set in report_to_urls.items():
        for url in urls_set:
            url_to_agg[url]["couples_total"] += 1

    for (rid, url), data in enriched_by_couple.items():
        agg = url_to_agg[url]
        agg["couples_enriched"] += 1
        if data.get("sourceCategory"):
            agg["categories"].add(data["sourceCategory"])
        for b in (data.get("detectedBrands") or []):
            if b.get("count", 0) <= 0:
                continue
            name = b.get("name") or "?"
            if b.get("isBrand"):
                agg["has_own"] = True
                agg["own_brands"].add(name)
                agg["own_count_total"] += b["count"]
            else:
                agg["has_comp"] = True
                agg["comp_brands"].add(name)
                agg["comp_count_total"] += b["count"]

    # Step 4: classify + filter + cross matrix
    all_records: list[dict] = []
    matrix: dict = defaultdict(lambda: defaultdict(int))

    for url, agg in url_to_agg.items():
        ownership, brand_status = _classify_url(url, agg, owned_patterns)
        matrix[ownership][brand_status] += 1

        if ownership_filter != "all" and ownership != ownership_filter:
            continue
        if brand_status not in wanted_statuses:
            continue

        all_records.append({
            "url": url,
            "domain": domain_from_url(url) or "",
            "ownership": ownership,
            "brand_status": brand_status,
            "own_brands": sorted(agg["own_brands"]),
            "comp_brands": sorted(agg["comp_brands"]),
            "own_count": agg["own_count_total"],
            "comp_count": agg["comp_count_total"],
            "category": " | ".join(sorted(agg["categories"])) or None,
            "couples": f"{agg['couples_enriched']}/{agg['couples_total']}",
        })

    STATUS_ORDER = {"own_only": 0, "own+comp": 1, "comp_only": 2, "no_brand": 3, "not_enriched": 4}
    all_records.sort(key=lambda r: (
        STATUS_ORDER.get(r["brand_status"], 99),
        -(r["own_count"] + r["comp_count"]),
    ))

    statuses = ["own_only", "own+comp", "comp_only", "no_brand", "not_enriched"]
    matrix_rows = []
    for own in ["owned", "external"]:
        row: dict[str, Any] = {"ownership": own}
        total = 0
        for s in statuses:
            v = matrix[own].get(s, 0)
            row[s] = v
            total += v
        row["TOTAL"] = total
        matrix_rows.append(row)
    total_row: dict[str, Any] = {"ownership": "TOTAL"}
    for s in statuses:
        total_row[s] = sum(matrix[o].get(s, 0) for o in ("owned", "external"))
    total_row["TOTAL"] = sum(total_row[s] for s in statuses)
    matrix_rows.append(total_row)

    return {
        "status": "success",
        "classified_urls": all_records[:top_n * 10],
        "matrix": matrix_rows,
        "metadata": {
            "topics_n": len(topics), "raw_total": len(all_raw),
            "after_response_filter": len(responses),
            "unique_urls": len(url_to_agg),
            "couples_total": sum(len(s) for s in report_to_urls.values()),
            "couples_enriched": len(enriched_by_couple),
            "brand_name": brand_name, "owned_patterns": owned_patterns,
            "filters": {
                "response_brand_mentioned": response_brand_mentioned,
                "ownership_filter": ownership_filter,
                "brand_status_filter": sorted(wanted_statuses),
                "models": models,
            },
        },
    }


# ══════════════════════════════════════════════════════════════════
# TOOL 7/7 — mint_enrich_sources
# ══════════════════════════════════════════════════════════════════

async def _tool_enrich_sources(args: dict) -> dict:
    """Batch-enrich URLs: DataForSEO category + detected brands per page."""
    domain_id = require_str(args, "domainId")
    report_id = require_str(args, "reportId")
    urls = args.get("urls")
    if not urls or not isinstance(urls, list) or len(urls) == 0:
        raise InvalidInput("'urls' is required and must be a non-empty list of URL strings.")
    if len(urls) > 1000:
        raise InvalidInput(f"Too many URLs ({len(urls)}). Maximum recommended: 1000 per call.")

    topic_id = optional_str(args, "topicId")
    brand_name_arg = optional_str(args, "brand_name")

    enriched: dict = {}
    for i in range(0, len(urls), 100):
        chunk = urls[i:i + 100]
        body: dict[str, Any] = {"urls": chunk, "reportId": report_id}
        if topic_id:
            body["topicId"] = topic_id
        try:
            resp = await fetch_post(f"/domains/{domain_id}/sources/enrichment", body)
            enriched.update(resp)
        except Exception as e:
            logger.warning("Enrich chunk failed: %s", e)

    omitted = [u for u in urls if u not in enriched]
    own_hits = comp_hits = 0
    for data in enriched.values():
        for b in (data.get("detectedBrands") or []):
            if b.get("isBrand"):
                own_hits += b.get("count", 0)
            else:
                comp_hits += b.get("count", 0)

    ownership = None
    if brand_name_arg:
        patterns = get_owned_patterns(brand_name_arg)
        ownership = {u: ("owned" if is_owned_domain(u, patterns) else "external") for u in urls}

    return {
        "status": "success",
        "enriched": enriched,
        "omitted": omitted,
        "ownership": ownership,
        "stats": {
            "total": len(urls), "enriched": len(enriched), "omitted": len(omitted),
            "own_hits": own_hits, "comp_hits": comp_hits,
            "coverage_pct": round(100 * len(enriched) / max(len(urls), 1), 1),
        },
        "metadata": {"reportId": report_id, "topicId": topic_id, "brand_name": brand_name_arg},
    }


# ══════════════════════════════════════════════════════════════════
# MCP TOOL DECLARATIONS
# ══════════════════════════════════════════════════════════════════

TOOL_DEFINITIONS: list[tuple[str, str, dict, dict]] = [
    # (name, description, inputSchema, annotations)

    (
        "mint_get_domains_and_topics",
        (
            "CATALOG DISCOVERY — lists all available domains (brands) and topics (markets). "
            "Call this FIRST to get the IDs required by other tools, or to show the user "
            "which brands/markets they can analyze."
            "\n\n"
            "USE FOR: 'What brands do I have?', 'List my topics', 'Show all IBIS markets', "
            "or to look up a domainId/topicId before calling another tool."
            "\n\n"
            "Returns: domains, topics, mapping 'Brand > Topic' to IDs, errors."
        ),
        {"type": "object", "properties": {}},
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),

    (
        "mint_get_topic_scores",
        (
            "DETAILED SCORES FOR 1 TOPIC — day-by-day Brand vs Competitors history, "
            "broken down by AI model. Makes 1 GLOBAL call + 1 call per model in parallel."
            "\n\n"
            "USE FOR: 'IBIS FR score evolution vs competitors', "
            "'Compare GPT-5.1 vs Sonar Pro on Novotel UK', detailed single-topic deep dive."
            "\n\n"
            "DON'T USE FOR:\n"
            "  Multi-topic summary -> mint_get_scores_overview\n"
            "  Time series chart -> mint_get_visibility_trend\n"
            "  Source analysis -> mint_get_topic_sources or mint_get_raw_responses"
            "\n\n"
            "Available models: GLOBAL, gpt-5.1, sonar-pro, google-ai-overview, "
            "gemini-3-pro-preview, gpt-5, gpt-interface."
            "\n\n"
            "GOLDEN RULE: if the user does NOT mention dates or models, OMIT those params. "
            "Defaults: last 30 days, all models."
        ),
        {
            "type": "object",
            "properties": {
                "domainId":  {"type": "string", "description": "Domain ID (REQUIRED). Get from mint_get_domains_and_topics."},
                "topicId":   {"type": "string", "description": "Topic ID (REQUIRED). Get from mint_get_domains_and_topics."},
                "startDate": {"type": "string", "description": "Start date YYYY-MM-DD (optional, default: 30 days ago)."},
                "endDate":   {"type": "string", "description": "End date YYYY-MM-DD (optional, default: today)."},
                "models":    {"type": "string", "description": "Comma-separated model filter, NO spaces. E.g. 'gpt-5.1,sonar-pro'. Omit for all models."},
            },
            "required": ["domainId", "topicId"],
        },
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),

    (
        "mint_get_scores_overview",
        (
            "MULTI-TOPIC SUMMARY TABLE — average visibility score per topic over a period, "
            "returned as a compact Markdown table. Self-contained: fetches topics automatically "
            "via brand_filter / market_filter."
            "\n\n"
            "USE FOR: 'Compare all IBIS markets in January', 'Q1 2026 overview all brands', "
            "'Which market performs best?'."
            "\n\n"
            "DON'T USE FOR:\n"
            "  Day-by-day history -> mint_get_topic_scores\n"
            "  Time series chart -> mint_get_visibility_trend\n"
            "  Source analysis -> mint_get_raw_responses"
            "\n\n"
            "TIP: brand_filter ('IBIS') and market_filter ('FR') reduce API calls. "
            "Combine both for precise targeting."
            "\n\n"
            "GOLDEN RULE: omit startDate/endDate/models when not specified by user. "
            "Defaults: last 90 days, all models."
        ),
        {
            "type": "object",
            "properties": {
                "brand_filter":  {"type": "string", "description": "Filter by brand name (e.g. 'IBIS', 'Mercure', 'Fairmont'). Optional."},
                "market_filter": {"type": "string", "description": "Filter by market keyword in topic name (e.g. 'FR', 'UK', 'DE'). Optional."},
                "topic_ids":     {"type": "array", "items": {"type": "string"}, "description": "Explicit list of topicIds. Optional."},
                "startDate":     {"type": "string", "description": "Start date YYYY-MM-DD (default: 90 days ago)."},
                "endDate":       {"type": "string", "description": "End date YYYY-MM-DD (default: today)."},
                "models":        {"type": "string", "description": "Comma-separated model filter. Omit for cross-model average."},
            },
            "required": [],
        },
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),

    (
        "mint_get_visibility_trend",
        (
            "BINNED TIME SERIES — visibility scores aggregated by day/week/month, "
            "ready for line charts. Returns {series: [{name, points: [{date, score, n}]}]}."
            "\n\n"
            "When you receive this data, GENERATE a line chart in a Claude artifact (Recharts, etc.)."
            "\n\n"
            "USE FOR: 'Weekly IBIS visibility curve since January', "
            "'Monthly trend chart for Q1', "
            "'Compare IBIS FR vs UK over 6 months' (aggregation='per_topic')."
            "\n\n"
            "DON'T USE if the user just wants numbers without a chart "
            "-> mint_get_scores_overview or mint_get_topic_scores."
            "\n\n"
            "KEY PARAMS:\n"
            "  granularity='week' (default): 1 point per Monday\n"
            "  aggregation='average' (default): single averaged series\n"
            "  aggregation='per_topic': one series per topic for visual comparison\n"
            "\n"
            "Default period: Jan 1st of current year to today."
        ),
        {
            "type": "object",
            "properties": {
                "brand_filter":  {"type": "string", "description": "Filter by brand name. Optional."},
                "market_filter": {"type": "string", "description": "Filter by market keyword. Optional."},
                "topic_ids":     {"type": "array", "items": {"type": "string"}, "description": "Explicit topicId list. Optional."},
                "startDate":     {"type": "string", "description": "YYYY-MM-DD (default: Jan 1st current year)."},
                "endDate":       {"type": "string", "description": "YYYY-MM-DD (default: today)."},
                "models":        {"type": "string", "description": "Comma-separated model filter. Optional."},
                "granularity":   {"type": "string", "enum": ["day", "week", "month"], "description": "Time bin size. Default: 'week'."},
                "aggregation":   {"type": "string", "enum": ["average", "per_topic"], "description": "'average' = single series, 'per_topic' = one per topic. Default: 'average'."},
            },
            "required": [],
        },
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),

    (
        "mint_get_topic_sources",
        (
            "TOP CITED DOMAINS & URLs — for 1 topic, per AI model. "
            "Uses Mint's pre-aggregated API (fast). "
            "1 GLOBAL call + 1 call per model in parallel."
            "\n\n"
            "USE FOR: 'Which websites are most cited for IBIS FR?', "
            "'Compare sources between GPT-5.1 and Sonar Pro on Novotel UK', "
            "'Citation evolution over time for this topic'."
            "\n\n"
            "DON'T USE FOR:\n"
            "  Classify sources (who mentions my brand vs competitors) -> mint_get_raw_responses\n"
            "  Category / brand detection enrichment -> mint_get_raw_responses or mint_enrich_sources"
            "\n\n"
            "Returns: top_domains, top_urls, domains_over_time, urls_over_time, global_metrics."
        ),
        {
            "type": "object",
            "properties": {
                "domainId":  {"type": "string", "description": "Domain ID (REQUIRED)."},
                "topicId":   {"type": "string", "description": "Topic ID (REQUIRED)."},
                "startDate": {"type": "string", "description": "YYYY-MM-DD (default: 90 days ago)."},
                "endDate":   {"type": "string", "description": "YYYY-MM-DD (default: today)."},
                "models":    {"type": "string", "description": "Comma-separated model filter. Optional."},
            },
            "required": ["domainId", "topicId"],
        },
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),

    (
        "mint_get_raw_responses",
        (
            "CORE SOURCE ANALYSIS — classifies every cited URL on 2 INDEPENDENT axes:"
            "\n\n"
            "AXIS 1 — ownership (domain regex, independent of crawl):\n"
            "  'owned' = URL on a brand-owned domain (e.g. all.accor.com for IBIS)\n"
            "  'external' = URL on a third-party domain (booking.com, tripadvisor.com...)"
            "\n\n"
            "AXIS 2 — brand_status (via /sources/enrichment crawl):\n"
            "  'own_only' = page mentions your brand only\n"
            "  'own+comp' = page mentions your brand AND competitors\n"
            "  'comp_only' = page mentions only competitors\n"
            "  'no_brand' = crawl ran but detected no brand\n"
            "  'not_enriched' = no crawl data available"
            "\n\n"
            "TYPICAL USE CASES:\n"
            "  1) 'Sources that also cite competitors when IBIS is mentioned'\n"
            "     -> response_brand_mentioned='true', brand_status_filter=['comp_only','own+comp']\n"
            "  2) 'External sources mentioning my brand'\n"
            "     -> ownership_filter='external', brand_status_filter=['own_only','own+comp']\n"
            "  3) 'Sources citing only competitors'\n"
            "     -> brand_status_filter='comp_only'\n"
            "  4) 'My owned URLs appearing in LLM responses'\n"
            "     -> ownership_filter='owned'\n"
            "  5) 'When IBIS is NOT cited, who takes my place?'\n"
            "     -> response_brand_mentioned='false', aggregate='sources'"
            "\n\n"
            "3 MODES via aggregate:\n"
            "  'classified' (default) = full 2-axis classification + cross matrix\n"
            "  'sources' = top domains/URLs without enrichment (faster)\n"
            "  'none' = raw responses for drill-down"
            "\n\n"
            "Strictly respects (reportId, url) coupling required by Mint API."
        ),
        {
            "type": "object",
            "properties": {
                "domainId":       {"type": "string", "description": "Domain ID (REQUIRED)."},
                "topic_ids":      {"type": "array", "items": {"type": "string"}, "description": "TopicId list. Optional (defaults to all topics for this domain)."},
                "brand_filter":   {"type": "string", "description": "Filter by brand name. Optional."},
                "market_filter":  {"type": "string", "description": "Filter by market keyword. Optional."},
                "startDate":      {"type": "string", "description": "YYYY-MM-DD. Optional."},
                "endDate":        {"type": "string", "description": "YYYY-MM-DD. Optional."},
                "models":         {"type": "string", "description": "Comma-separated model filter. Optional."},
                "latestOnly":     {"type": "boolean", "description": "If true, only use the most recent report (ignores dates). Default: false."},
                "response_brand_mentioned": {
                    "type": "string", "enum": ["true", "false", "all"],
                    "description": "RESPONSE-level filter: is the brand mentioned in the LLM response? Default: 'all'.",
                },
                "aggregate": {
                    "type": "string", "enum": ["classified", "sources", "none"],
                    "description": "Output mode. Default: 'classified'.",
                },
                "ownership_filter": {
                    "type": "string", "enum": ["owned", "external", "all"],
                    "description": "Axis 1 filter. Default: 'all'.",
                },
                "brand_status_filter": {
                    "oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
                    "description": "Axis 2 filter: one value or list from: own_only, own+comp, comp_only, no_brand, not_enriched. Default: all.",
                },
                "top_n": {"type": "integer", "description": "Max URLs returned. Default: 30, max: 500."},
            },
            "required": ["domainId"],
        },
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    ),

    (
        "mint_enrich_sources",
        (
            "BATCH URL ENRICHMENT — for a list of URLs + a reportId, returns "
            "DataForSEO category + detected brands (isBrand=true = own, false = competitor). "
            "Auto-chunks at 100 URLs (API limit)."
            "\n\n"
            "USE FOR: debugging specific URLs, custom workflows outside the raw_responses flow, "
            "manually enriching a known batch of URLs."
            "\n\n"
            "WARNING: For typical analysis (who cites my brand / competitors), prefer "
            "mint_get_raw_responses(aggregate='classified') which automates the full pipeline."
            "\n\n"
            "Returns: enriched (URL to {sourceCategory, detectedBrands}), omitted, stats, "
            "ownership (if brand_name provided)."
        ),
        {
            "type": "object",
            "properties": {
                "domainId":   {"type": "string", "description": "Domain ID (REQUIRED)."},
                "urls":       {"type": "array", "items": {"type": "string"}, "description": "URLs to enrich (REQUIRED, max 1000)."},
                "reportId":   {"type": "string", "description": "Report ID (REQUIRED — API indexes by (reportId, url))."},
                "topicId":    {"type": "string", "description": "Topic ID to resolve market/language. Optional."},
                "brand_name": {"type": "string", "description": "If provided, adds owned/external classification."},
            },
            "required": ["domainId", "urls", "reportId"],
        },
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),
]


# ══════════════════════════════════════════════════════════════════
# MCP REGISTRATION — list_tools + call_tool
# ══════════════════════════════════════════════════════════════════

_TOOL_HANDLERS: dict[str, Any] = {
    # v4.1.0 prefixed names
    "mint_get_domains_and_topics": _tool_get_domains_and_topics,
    "mint_get_topic_scores":       _tool_get_topic_scores,
    "mint_get_scores_overview":    _tool_get_scores_overview,
    "mint_get_visibility_trend":   _tool_get_visibility_trend,
    "mint_get_topic_sources":      _tool_get_topic_sources,
    "mint_get_raw_responses":      _tool_get_raw_responses,
    "mint_enrich_sources":         _tool_enrich_sources,
    # v4.0.0 backward-compat aliases (same handlers)
    "get_domains_and_topics": _tool_get_domains_and_topics,
    "get_topic_scores":       _tool_get_topic_scores,
    "get_scores_overview":    _tool_get_scores_overview,
    "get_visibility_trend":   _tool_get_visibility_trend,
    "get_topic_sources":      _tool_get_topic_sources,
    "get_raw_responses":      _tool_get_raw_responses,
    "enrich_sources":         _tool_enrich_sources,
}


@server.list_tools()
async def list_tools() -> list[Tool]:
    """Declare all 7 tools with descriptions, schemas, and annotations."""
    return [
        Tool(
            name=name,
            description=desc,
            inputSchema={**schema, "x-annotations": annotations},
        )
        for name, desc, schema, annotations in TOOL_DEFINITIONS
    ]


@server.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    """MCP dispatcher: routes, validates, handles errors, serializes."""
    fn = _TOOL_HANDLERS.get(name)
    if fn is None:
        return [TextContent(
            type="text",
            text=json.dumps({
                "status": "error",
                "message": f"Unknown tool: '{name}'. Available: {sorted(set(t[0] for t in TOOL_DEFINITIONS))}.",
            }),
        )]

    try:
        result = await fn(arguments or {})
    except InvalidInput as e:
        result = {"status": "error", "error_type": "invalid_input", "message": str(e)}
    except AuthError as e:
        result = {"status": "error", "error_type": "auth", "message": str(e), "status_code": e.status_code}
    except NotFoundError as e:
        result = {"status": "error", "error_type": "not_found", "message": str(e), "status_code": e.status_code}
    except RateLimitError as e:
        result = {"status": "error", "error_type": "rate_limit", "message": str(e), "status_code": e.status_code}
    except MintAPIError as e:
        result = {"status": "error", "error_type": "api", "message": str(e), "status_code": e.status_code}
    except TypeError as e:
        result = {"status": "error", "error_type": "invalid_arguments", "message": str(e)}
    except Exception as e:
        logger.exception("Tool %s crashed unexpectedly", name)
        result = {"status": "error", "error_type": "internal", "message": f"{type(e).__name__}: {e}"}

    return [TextContent(
        type="text",
        text=json.dumps(result, default=str, ensure_ascii=False),
    )]


# ══════════════════════════════════════════════════════════════════
# WEB TRANSPORT (SSE — compatible Render + Claude.ai)
# ══════════════════════════════════════════════════════════════════

sse = SseServerTransport("/messages")


async def handle_sse_connect(request: Request):
    """Handle initial SSE connection (GET). Stays open for the MCP session."""
    async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
        await server.run(streams[0], streams[1], server.create_initialization_options())


async def handle_messages(request: Request):
    """Handle JSON-RPC messages (POST)."""
    await sse.handle_post_message(request.scope, request.receive, request._send)


async def handle_health(_request: Request):
    """Health check endpoint for Render / Koyeb / Docker."""
    return JSONResponse({
        "status": "ok",
        "service": "mint_visibility_mcp",
        "version": __version__,
        "tools": len(TOOL_DEFINITIONS),
    })


routes = [
    Route("/",         endpoint=handle_health,      methods=["GET"]),
    Route("/health",   endpoint=handle_health,      methods=["GET"]),
    Route("/sse",      endpoint=handle_sse_connect, methods=["GET"]),
    Route("/sse",      endpoint=handle_messages,    methods=["POST"]),
    Route("/messages", endpoint=handle_messages,    methods=["POST"]),
]

middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
]

@asynccontextmanager
async def _lifespan(app):
    """Manage persistent HTTP client lifecycle (startup/shutdown)."""
    await _start_http_client()
    yield
    await _stop_http_client()


app = Starlette(
    debug=False,
    routes=routes,
    middleware=middleware,
    lifespan=_lifespan,
)
