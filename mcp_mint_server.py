"""
Mint.ai Visibility MCP Server — v5.2.0 (Owned-domain fix + throttling)

═══════════════════════════════════════════════════════════════════
WHAT'S NEW IN v5.2.0
═══════════════════════════════════════════════════════════════════
FIX    — Owned/external classification now UNIONs each brand's patterns
         with the global "_default" list, so corporate domains (e.g.
         accor.com → all.accor.com, ibis.accor.com) are owned on EVERY
         topic. Ship owned_domains.json with a "_default" entry.
PERF   — HTTP_MIN_INTERVAL throttle: minimum delay between request starts
         (default 0.15s) to avoid overloading the Mint API.
CLEAN  — mint_get_topic_overview no longer returns domain_source_analysis
         (Mint reports brandDomainPercentage=0, so it was not useful).
═══════════════════════════════════════════════════════════════════

Mint.ai Visibility MCP Server — v5.1.0 (Macro topic overview)

═══════════════════════════════════════════════════════════════════
WHAT'S NEW IN v5.1.0
═══════════════════════════════════════════════════════════════════
NEW    — mint_get_topic_overview: one-call MACRO snapshot for a topic
         (averageScore + variation, share of voice, brand rank, per-model
         breakdown, competitors, topMentions, domain source mix). Surfaces
         shareOfVoice / topMentions / domainSourceAnalysis that no other tool
         exposed. Intentionally omits heavy time-series and full domain/URL
         lists (those stay in mint_get_topic_scores / mint_get_topic_sources),
         so no redundancy with the existing detail tools.
═══════════════════════════════════════════════════════════════════

Mint.ai Visibility MCP Server — v5.0.0 (Split source analysis)

═══════════════════════════════════════════════════════════════════
WHAT CHANGED IN v5.0.0 (from v4.2.0)
═══════════════════════════════════════════════════════════════════
SPLIT  — The overloaded mint_get_raw_responses is replaced by two clear tools:
           mint_get_response_sources  (FAST, no DataForSEO) and
           mint_enrich_cited_sources  (DEEP, DataForSEO page enrichment).
         The old tool stays registered as an unlisted backward-compat alias.
PERF   — Enrichment now runs only on the top_n most-cited URLs (ranked first),
         instead of enriching everything up front.
METRIC — Source counts are CITATION-WEIGHTED (citations / responses / unique_urls)
         instead of counting each unique URL once.
CLARITY— response_brand_mentioned (the LLM answer) and source_content_brand_status
         (the cited page content) are now separate, never conflated.
FIX    — Per-report topicId is tracked, so enrichment passes the correct topicId
         for each reportId (previously always topics[0]["topicId"]).
NEW    — mint_enrich_cited_sources.brand_citation_ranking: external sources ranked
         by how much each page actually cites your brand.
═══════════════════════════════════════════════════════════════════

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

Tools (9 exposed):
  mint_get_domains_and_topics  — catalog discovery
  mint_get_models_by_topic     — list AI models for one topic
  mint_get_topic_overview      — v5.1: one-call MACRO snapshot (score, SoV, competitors, source mix)
  mint_get_topic_scores        — Brand vs Competitors, 1 topic, per-model
  mint_get_scores_overview     — multi-topic summary table
  mint_get_visibility_trend    — time series for charts
  mint_get_topic_sources       — top cited domains/URLs, 1 topic
  mint_get_response_sources    — v5: FAST cited-source overview, weighted, no enrichment
  mint_enrich_cited_sources    — v5: DEEP page enrichment, ranks sources citing your brand

Unlisted but callable (backward compat): mint_get_raw_responses, mint_enrich_sources

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
import time
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
# Minimum delay (seconds) enforced between the START of any two API requests,
# to avoid hammering the Mint API. 0 disables throttling.
HTTP_MIN_INTERVAL: float = float(os.getenv("HTTP_MIN_INTERVAL", "0.15"))
# Hard ceiling for a single tool call. Kept below typical MCP client timeouts
# so the server returns a clean error BEFORE the client gives up / drops the connection.
TOOL_TIMEOUT: float = float(os.getenv("TOOL_TIMEOUT", "120.0"))

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
_RATE_LOCK = asyncio.Lock()
_last_request_ts: float = 0.0

__version__ = "5.2.0"

server = Server("mint_visibility_mcp")


# ══════════════════════════════════════════════════════════════════
# PERSISTENT HTTP CLIENT (connection pooling + TLS reuse)
# ══════════════════════════════════════════════════════════════════

_http_client: httpx.AsyncClient | None = None


async def _start_http_client() -> None:
    """Create persistent HTTP client on server startup."""
    global _http_client
    # Granular timeouts so a single stalled request fails fast instead of
    # blocking the whole tool for HTTP_TIMEOUT seconds:
    #   connect: short — if we can't reach the API quickly, fail and retry
    #   read:    full HTTP_TIMEOUT — the API can be slow to compute a response
    #   write/pool: bounded
    timeout = httpx.Timeout(
        connect=10.0,
        read=HTTP_TIMEOUT,
        write=15.0,
        pool=15.0,
    )
    _http_client = httpx.AsyncClient(
        timeout=timeout,
        limits=httpx.Limits(
            max_connections=HTTP_MAX_CONCURRENT + 2,
            max_keepalive_connections=HTTP_MAX_CONCURRENT,
        ),
    )
    logger.info("HTTP client started (read_timeout=%.0fs, pool=%d)", HTTP_TIMEOUT, HTTP_MAX_CONCURRENT)


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
    """Owned domain patterns for a brand.

    Always UNION the brand-specific patterns with the global "_default" list,
    so shared corporate domains (e.g. accor.com, which also covers
    all.accor.com and ibis.accor.com via subdomain matching) are treated as
    owned for EVERY topic/brand — not only when a brand-specific entry exists.
    """
    patterns = set(OWNED_DOMAINS_MAP.get("_default", []))
    patterns |= set(OWNED_DOMAINS_MAP.get(brand_name, []))
    return sorted(patterns)


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


async def _throttle() -> None:
    """Space out request *starts* by at least HTTP_MIN_INTERVAL seconds."""
    global _last_request_ts
    if HTTP_MIN_INTERVAL <= 0:
        return
    async with _RATE_LOCK:
        now = time.monotonic()
        wait = _last_request_ts + HTTP_MIN_INTERVAL - now
        if wait > 0:
            await asyncio.sleep(wait)
            now = time.monotonic()
        _last_request_ts = now


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
                await _throttle()
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
    """Fetch all domains (brands) and their topics (markets).

    Two-step flow:
      1. GET /domains             -> keep ONLY id + displayName (rest is too large)
      2. GET /domains/{id}/topics -> topicId + topicName per domain

    Returns a lightweight catalog (domains, topics, mapping, errors) that can be
    shown as a table to the user OR reused as IDs by the other tools.
    """
    raw_domains = await fetch_get("/domains")

    # Step 1: lightweight domains (id + displayName only)
    domains = [
        {
            "domainId": d.get("id"),
            "domainName": d.get("displayName") or d.get("name") or "Unknown",
        }
        for d in raw_domains
    ]

    topics, mapping, errors = [], {}, []

    # Step 2: topics per domain
    for d in domains:
        d_id = d["domainId"]
        d_name = d["domainName"]
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
# TOOL — mint_get_models_by_topic
# ══════════════════════════════════════════════════════════════════

async def _tool_get_models_by_topic(args: dict) -> dict:
    """List the AI models available for ONE topic.

    Each topic can have its own set of models, so this is resolved live from
    the topic's visibility endpoint rather than a hardcoded list.

    Call this only when the user asks to deep-dive a specific model — by default
    other tools return the GLOBAL (combined) view.
    """
    domain_id = require_str(args, "domainId")
    topic_id = require_str(args, "topicId")

    start_date, end_date = default_date_range(days=30)
    params = {
        "startDate": start_date, "endDate": end_date,
        "latestOnly": "false", "page": 1, "limit": 1,
    }
    endpoint = f"/domains/{domain_id}/topics/{topic_id}/visibility/aggregated"

    data = await fetch_get(endpoint, params)
    available = data.get("availableModels", []) or []

    return {
        "domainId": domain_id,
        "topicId": topic_id,
        "models": available,
        "count": len(available),
        "note": (
            "These are the models available for this topic. "
            "Pass one (or several, comma-separated, no spaces) to the 'models' "
            "param of mint_get_topic_scores or mint_get_topic_sources to deep-dive."
        ),
    }


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
# TOOL — mint_get_topic_overview (MACRO snapshot, single API call)
# ══════════════════════════════════════════════════════════════════
#
# One call to /visibility/aggregated → the macro KPIs only. Deliberately
# does NOT fan out per model and does NOT return the heavy time-series
# (chartData, topDomainsOverTime, topUrlsOverTime) nor the full domain/URL
# lists — those stay the job of mint_get_topic_scores / mint_get_topic_sources.
# Surfaces fields no other tool exposes: shareOfVoice, topMentions,
# domainSourceAnalysis.

async def _tool_get_topic_overview(args: dict) -> dict:
    """Macro snapshot for ONE topic in a single API call (no per-model fan-out)."""
    domain_id = require_str(args, "domainId")
    topic_id = require_str(args, "topicId")
    start_date = optional_str(args, "startDate")
    end_date = optional_str(args, "endDate")
    models = optional_str(args, "models")
    top_n = optional_int(args, "top_n", default=10, min_val=1, max_val=100)
    include_model_breakdown = optional_bool(args, "include_model_breakdown", True)
    use_all_models_for_competitors = optional_bool(args, "useAllModelsForCompetitors", False)

    if not start_date or not end_date:
        start_date, end_date = default_date_range(days=30)

    params: dict[str, Any] = {
        "startDate": start_date, "endDate": end_date,
        "includeVariation": "true",
        "useAllModelsForCompetitors": "true" if use_all_models_for_competitors else "false",
        "page": 1, "limit": 1,
    }
    if models:
        params["models"] = models

    endpoint = f"/domains/{domain_id}/topics/{topic_id}/visibility/aggregated"
    data = await fetch_get(endpoint, params)

    # ─── Share of voice summary from chartData (heavy array NOT returned) ───
    chart = data.get("chartData") or []
    sov_series = sorted(
        [(c.get("date"), c.get("shareOfVoice")) for c in chart if c.get("shareOfVoice") is not None],
        key=lambda x: x[0] or "",
    )
    share_of_voice = None
    if sov_series:
        vals = [v for _, v in sov_series]
        share_of_voice = {
            "latest": sov_series[-1][1],
            "latest_date": sov_series[-1][0],
            "first": sov_series[0][1],
            "average": round(sum(vals) / len(vals), 2),
            "change": round(sov_series[-1][1] - sov_series[0][1], 2),
            "points_n": len(vals),
        }

    # ─── Competitors (score + variation, optional per-model breakdown) ───
    competitors = []
    for c in (data.get("competitors") or []):
        entry: dict[str, Any] = {
            "name": c.get("name"),
            "averageScore": c.get("averageScore"),
            "variation": c.get("variation"),
        }
        if include_model_breakdown:
            entry["modelBreakdown"] = c.get("modelBreakdown") or []
        competitors.append(entry)
    competitors.sort(key=lambda x: -(x["averageScore"] or 0))

    # ─── Brand rank among all entities by score ───
    brand_score = data.get("averageScore")
    ranking = sorted(
        [{"name": "__BRAND__", "score": brand_score}]
        + [{"name": c["name"], "score": c["averageScore"]} for c in competitors],
        key=lambda x: -(x["score"] if x["score"] is not None else -1),
    )
    brand_rank = next((i + 1 for i, e in enumerate(ranking) if e["name"] == "__BRAND__"), None)

    # ─── Normalize dateRange (the API sometimes returns start/end reversed) ───
    dr = data.get("dateRange") or {}
    ds, de = dr.get("start"), dr.get("end")
    if ds and de and ds > de:
        ds, de = de, ds

    available_models = data.get("availableModels") or []

    return {
        "status": "success",
        "kpis": {
            "averageScore": brand_score,
            "scoreVariation": data.get("scoreVariation"),
            "brand_rank": brand_rank,
            "entities_ranked": len(ranking),
            "share_of_voice": share_of_voice,
            "totalPromptsTested": data.get("totalPromptsTested"),
            "totalCitations": data.get("totalCitations"),
            "reportCount": data.get("reportCount"),
        },
        "available_models": available_models,
        "models_count": len(available_models),
        "model_breakdown": (data.get("modelBreakdown") or []) if include_model_breakdown else None,
        "competitors": competitors,
        "top_mentions": (data.get("topMentions") or [])[:top_n],
        "next_step": {
            "for_top_domains_and_urls": "mint_get_topic_sources",
            "for_score_time_series": "mint_get_topic_scores",
            "for_a_chart": "mint_get_visibility_trend",
            "for_source_brand_analysis": "mint_get_response_sources / mint_enrich_cited_sources",
        },
        "metadata": {
            "domainId": domain_id, "topicId": topic_id,
            "dateRange": {"start": ds, "end": de},
            "requested": {"startDate": start_date, "endDate": end_date},
            "models": models or "all (GLOBAL)",
            "useAllModelsForCompetitors": use_all_models_for_competitors,
            "note": "Single-call macro snapshot. Heavy time-series and full "
                    "domain/URL lists are intentionally omitted — use the detail "
                    "tools listed in next_step.",
        },
    }


# ══════════════════════════════════════════════════════════════════
# SHARED — raw-results collection (used by the two source tools below)
# ══════════════════════════════════════════════════════════════════
#
# The old monolithic mint_get_raw_responses did EVERYTHING in one call (fetch,
# rank, enrich, classify, build matrix). It is split into two clearer tools:
#
#   mint_get_response_sources  — FAST. No DataForSEO. Answers "who is cited,
#                                owned vs external, brand mentioned or not".
#   mint_enrich_cited_sources  — DEEP. DataForSEO enrichment. Answers "do the
#                                cited PAGES themselves talk about my brand,
#                                and which external sources cite it the most".
#
# Two distinct notions are kept strictly separate (they were conflated before):
#   response_brand_mentioned     -> the LLM ANSWER mentions the brand (raw field)
#   source_content_brand_status  -> the cited PAGE content mentions the brand
#                                    (only known after enrichment)

async def _collect_raw_results(
    domain_id: str,
    topics: list,
    params: dict,
    response_brand_mentioned: str = "all",
) -> tuple[list, list, dict]:
    """Fetch raw-results for the given topics and return
    (all_raw, responses, report_to_topic_id).

    Every result is tagged with the topic it was fetched under, so
    report_to_topic_id maps each reportId to the CORRECT topicId for enrichment
    (fixes the previous bug where topics[0]["topicId"] was always used).
    """
    async def fetch_topic(t):
        results = await _fetch_raw_one_topic(t["domainId"], t["topicId"], params)
        for r in results:
            r["_topicId"] = t["topicId"]
            r["_topicName"] = t["topicName"]
            r["_domainName"] = t["domainName"]
        return results

    fetched = await asyncio.gather(*[fetch_topic(t) for t in topics])
    all_raw = [r for batch in fetched for r in batch]

    report_to_topic_id: dict[str, str] = {}
    for r in all_raw:
        rid = r.get("reportId")
        if rid and rid not in report_to_topic_id:
            report_to_topic_id[rid] = r.get("_topicId")

    if response_brand_mentioned == "true":
        responses = [r for r in all_raw if r.get("brandMentioned") is True]
    elif response_brand_mentioned == "false":
        responses = [r for r in all_raw if r.get("brandMentioned") is False]
    else:
        responses = all_raw

    return all_raw, responses, report_to_topic_id


def _resolve_domain_topics(catalog: dict, domain_id: str,
                           topic_ids=None, brand_filter=None, market_filter=None) -> list:
    """Filter the catalog down to topics belonging to domain_id."""
    topics = filter_topics(catalog["topics"], topic_ids, brand_filter, market_filter)
    return [t for t in topics if t["domainId"] == domain_id]


# ══════════════════════════════════════════════════════════════════
# TOOL — mint_get_response_sources (FAST, no enrichment)
# ══════════════════════════════════════════════════════════════════

async def _tool_get_response_sources(args: dict) -> dict:
    """Fast cited-source overview from raw responses. NO DataForSEO.

    Weighted metrics (citations / responses / unique_urls) — not just a raw URL
    count — so a URL cited 80 times no longer weighs the same as one cited once.
    Splits everything on two response-level axes: ownership (owned/external) and
    response_brand_mentioned (was the brand named in the LLM answer).
    """
    domain_id = require_str(args, "domainId")
    topic_ids = optional_str_list(args, "topic_ids")
    brand_filter = optional_str(args, "brand_filter")
    market_filter = optional_str(args, "market_filter")
    start_date = optional_str(args, "startDate")
    end_date = optional_str(args, "endDate")
    models = optional_str(args, "models")
    latest_only = optional_bool(args, "latestOnly", False)
    response_brand_mentioned = optional_enum(
        args, "response_brand_mentioned", {"true", "false", "all"}, "all")
    ownership_filter = optional_enum(args, "ownership_filter", {"owned", "external", "all"}, "all")
    top_n = optional_int(args, "top_n", default=30, min_val=1, max_val=500)

    catalog = await _tool_get_domains_and_topics({})
    topics = _resolve_domain_topics(catalog, domain_id, topic_ids, brand_filter, market_filter)
    if not topics:
        return {
            "status": "error",
            "message": f"No topics found for domainId={domain_id}. "
                       "Use mint_get_domains_and_topics to verify the domainId.",
        }

    brand_name = topics[0]["domainName"]
    owned_patterns = get_owned_patterns(brand_name)

    params: dict[str, Any] = {"limit": 100}
    if start_date:  params["startDate"] = start_date
    if end_date:    params["endDate"] = end_date
    if models:      params["models"] = models
    if latest_only: params["latestOnly"] = "true"

    all_raw, responses, _ = await _collect_raw_results(
        domain_id, topics, params, response_brand_mentioned)

    # ─── Weighted aggregation ───
    dom_cit: Counter = Counter()
    dom_resp: dict[str, set] = defaultdict(set)
    dom_urls: dict[str, set] = defaultdict(set)
    dom_own: dict[str, str] = {}

    url_cit: Counter = Counter()
    url_resp: dict[str, set] = defaultdict(set)
    url_reports: dict[str, set] = defaultdict(set)
    url_topics: dict[str, set] = defaultdict(set)
    url_own: dict[str, str] = {}

    tom_c: Counter = Counter()

    own_sum = {
        "owned":    {"citations": 0, "responses": set(), "urls": set()},
        "external": {"citations": 0, "responses": set(), "urls": set()},
    }
    bm_split = {"true": {"owned": 0, "external": 0}, "false": {"owned": 0, "external": 0}}
    matrix = {"owned": {"true": 0, "false": 0}, "external": {"true": 0, "false": 0}}

    for r in responses:
        rid = r.get("reportId")
        resp_id = r.get("id") or rid
        bm = "true" if r.get("brandMentioned") is True else "false"
        for c in (r.get("citations") or []):
            u = c.get("url")
            if not u:
                continue
            dom = c.get("website") or domain_from_url(u)
            if not dom:
                continue
            own_key = "owned" if is_owned_domain(u, owned_patterns) else "external"

            url_cit[u] += 1
            url_resp[u].add(resp_id)
            url_own[u] = own_key
            if rid:
                url_reports[u].add(rid)
            if r.get("_topicId"):
                url_topics[u].add(r["_topicId"])

            dom_cit[dom] += 1
            dom_resp[dom].add(resp_id)
            dom_urls[dom].add(u)
            dom_own[dom] = own_key

            own_sum[own_key]["citations"] += 1
            own_sum[own_key]["responses"].add(resp_id)
            own_sum[own_key]["urls"].add(u)
            bm_split[bm][own_key] += 1
            matrix[own_key][bm] += 1

        for b in (r.get("topOfMind") or []):
            tom_c[b] += 1

    # ─── Top URLs (weighted, carry reportIds so they can feed the deep tool) ───
    url_rows = []
    for u, c in url_cit.items():
        ok = url_own[u]
        if ownership_filter != "all" and ok != ownership_filter:
            continue
        url_rows.append({
            "url": u,
            "domain": domain_from_url(u) or "",
            "ownership": ok,
            "citations": c,
            "responses": len(url_resp[u]),
            "report_ids": sorted(url_reports[u])[:10],
            "topic_ids": sorted(url_topics[u]),
        })
    url_rows.sort(key=lambda x: (-x["citations"], -x["responses"]))
    top_urls = url_rows[:top_n]

    # ─── Top domains (weighted) ───
    dom_rows = []
    for d, c in dom_cit.items():
        ok = dom_own[d]
        if ownership_filter != "all" and ok != ownership_filter:
            continue
        dom_rows.append({
            "domain": d,
            "ownership": ok,
            "citations": c,
            "unique_urls": len(dom_urls[d]),
            "responses": len(dom_resp[d]),
        })
    dom_rows.sort(key=lambda x: (-x["citations"], -x["unique_urls"]))
    top_domains = dom_rows[:top_n]

    ownership_summary = {
        k: {"citations": v["citations"], "unique_urls": len(v["urls"]), "responses": len(v["responses"])}
        for k, v in own_sum.items()
    }

    brand_mentioned_split = {
        "brand_mentioned_true": {
            "owned_citations": bm_split["true"]["owned"],
            "external_citations": bm_split["true"]["external"],
        },
        "brand_mentioned_false": {
            "owned_citations": bm_split["false"]["owned"],
            "external_citations": bm_split["false"]["external"],
        },
    }

    # ─── Direct matrix: ownership × response_brand_mentioned (weighted by citations) ───
    matrix_rows = []
    for own in ("owned", "external"):
        t = matrix[own]["true"]
        f = matrix[own]["false"]
        matrix_rows.append({
            "ownership": own,
            "brand_mentioned_true": t,
            "brand_mentioned_false": f,
            "TOTAL": t + f,
        })
    tot_t = sum(matrix[o]["true"] for o in ("owned", "external"))
    tot_f = sum(matrix[o]["false"] for o in ("owned", "external"))
    matrix_rows.append({
        "ownership": "TOTAL",
        "brand_mentioned_true": tot_t,
        "brand_mentioned_false": tot_f,
        "TOTAL": tot_t + tot_f,
    })

    # ─── Ready-to-enrich payload for the top EXTERNAL URLs (feeds the deep tool) ───
    next_step_sources = [
        {"url": r["url"], "reportId": r["report_ids"][0],
         "topicId": (r["topic_ids"][0] if r["topic_ids"] else None)}
        for r in top_urls
        if r["ownership"] == "external" and r["report_ids"]
    ][:100]

    return {
        "status": "success",
        "top_domains": top_domains,
        "top_urls": top_urls,
        "top_of_mind": [{"brand": b, "count": c} for b, c in tom_c.most_common(top_n)],
        "ownership_summary": ownership_summary,
        "brand_mentioned_split": brand_mentioned_split,
        "matrix": matrix_rows,
        "next_step": {
            "tool": "mint_enrich_cited_sources",
            "why": "Enrich these external URLs to learn which pages actually mention your brand.",
            "sources": next_step_sources,
        },
        "metadata": {
            "brand_name": brand_name,
            "owned_patterns": owned_patterns,
            "topics_n": len(topics),
            "raw_total": len(all_raw),
            "after_response_filter": len(responses),
            "unique_urls": len(url_cit),
            "unique_domains": len(dom_cit),
            "filters": {
                "response_brand_mentioned": response_brand_mentioned,
                "ownership_filter": ownership_filter,
                "models": models,
            },
            "note": "Counts are CITATION-weighted (a URL cited N times counts N). "
                    "This tool does NOT inspect page content — use "
                    "mint_enrich_cited_sources for source_content_brand_status.",
        },
    }


# ══════════════════════════════════════════════════════════════════
# TOOL — mint_enrich_cited_sources (DEEP, DataForSEO enrichment)
# ══════════════════════════════════════════════════════════════════

def _source_content_status(agg: dict) -> str:
    """Classify a URL by what its PAGE CONTENT mentions (post-enrichment)."""
    if agg["couples_enriched"] == 0:
        return "not_enriched"
    if agg["has_own"] and agg["has_comp"]:
        return "own+comp"
    if agg["has_own"]:
        return "own_only"
    if agg["has_comp"]:
        return "comp_only"
    return "no_brand"


async def _tool_enrich_cited_sources(args: dict) -> dict:
    """Enrich cited URLs (DataForSEO) and detect brand vs competitors in the
    PAGE CONTENT. Answers 'which external sources cite my brand the most'.

    Two input modes:
      AUTO     (default) — give domainId + topic selection; the tool fetches raw
                 responses, ranks cited URLs by citation weight, keeps top_n
                 (external by default), then enriches only those. Cheap & fast.
      EXPLICIT — give a 'sources' list of {url, reportId, topicId?} to enrich
                 an exact set (e.g. the next_step.sources from the fast tool).
    """
    domain_id = require_str(args, "domainId")
    explicit = args.get("sources")
    brand_name_arg = optional_str(args, "brand_name")
    top_n = optional_int(args, "top_n", default=50, min_val=1, max_val=300)
    crawl_all = optional_bool(args, "crawl_all", False)
    max_reports_per_url = optional_int(args, "max_reports_per_url", default=3, min_val=1, max_val=50)
    source_scope = optional_enum(args, "source_scope", {"external", "owned", "all"}, "external")
    response_brand_mentioned = optional_enum(
        args, "response_brand_mentioned", {"true", "false", "all"}, "all")

    catalog = await _tool_get_domains_and_topics({})
    domain_topics = [t for t in catalog["topics"] if t["domainId"] == domain_id]
    brand_name = brand_name_arg or (domain_topics[0]["domainName"] if domain_topics else domain_id)
    owned_patterns = get_owned_patterns(brand_name)

    url_cit: Counter = Counter()  # only populated in AUTO mode (for weighting)
    report_urls: dict[str, list] = defaultdict(list)
    report_topic: dict[str, str] = {}
    mode = "explicit" if explicit else "auto"
    scanned = 0

    if mode == "explicit":
        if not isinstance(explicit, list) or not explicit:
            raise InvalidInput("'sources' must be a non-empty list of {url, reportId, topicId?} objects.")
        for s in explicit:
            if not isinstance(s, dict):
                raise InvalidInput("Each item in 'sources' must be an object with 'url' and 'reportId'.")
            u = s.get("url")
            rid = s.get("reportId")
            if not u or not rid:
                raise InvalidInput("Each source needs both 'url' and 'reportId'.")
            report_urls[rid].append(u)
            if s.get("topicId"):
                report_topic[rid] = s["topicId"]
    else:
        topic_ids = optional_str_list(args, "topic_ids")
        market_filter = optional_str(args, "market_filter")
        brand_filter = optional_str(args, "brand_filter")
        start_date = optional_str(args, "startDate")
        end_date = optional_str(args, "endDate")
        models = optional_str(args, "models")
        latest_only = optional_bool(args, "latestOnly", False)

        topics = _resolve_domain_topics(catalog, domain_id, topic_ids, brand_filter, market_filter)
        if not topics:
            return {
                "status": "error",
                "message": f"No topics found for domainId={domain_id}. "
                           "Use mint_get_domains_and_topics to verify the domainId.",
            }

        params: dict[str, Any] = {"limit": 100}
        if start_date:  params["startDate"] = start_date
        if end_date:    params["endDate"] = end_date
        if models:      params["models"] = models
        if latest_only: params["latestOnly"] = "true"

        _, responses, report_to_topic_id = await _collect_raw_results(
            domain_id, topics, params, response_brand_mentioned)
        scanned = len(responses)
        report_topic = report_to_topic_id

        url_reports_tmp: dict[str, set] = defaultdict(set)
        for r in responses:
            rid = r.get("reportId")
            for c in (r.get("citations") or []):
                u = c.get("url")
                if not u:
                    continue
                ok = "owned" if is_owned_domain(u, owned_patterns) else "external"
                if source_scope != "all" and ok != source_scope:
                    continue
                url_cit[u] += 1
                if rid:
                    url_reports_tmp[u].add(rid)

        ranked = [u for u, _ in url_cit.most_common(None if crawl_all else top_n)]
        for u in ranked:
            for rid in url_reports_tmp[u]:
                report_urls[rid].append(u)

    if not report_urls:
        return {
            "status": "success",
            "classified_sources": [],
            "brand_citation_ranking": [],
            "matrix": [],
            "summary": {"own_only": 0, "own+comp": 0, "comp_only": 0, "no_brand": 0, "not_enriched": 0},
            "metadata": {"mode": mode, "brand_name": brand_name,
                         "message": "No URLs to enrich for the given scope."},
        }

    # ─── Optimised enrichment: each URL only needs ONE crawl hit ───
    # A page content is the same whatever report cites it, and crawl results are
    # stored per (reportId, url). Enriching every couple is hugely redundant
    # (same url x N reports). Instead we try each URL against ONE report at a time
    # and STOP at the first real crawl hit; URLs still missing crawl data fall back
    # to the next report that cites them, up to max_reports_per_url passes. This
    # collapses thousands of couples into a few hundred lookups, full coverage kept.
    def _has_crawl(payload) -> bool:
        return isinstance(payload, dict) and any(
            k in payload for k in ("contentLength", "wordCount", "detectedBrands",
                                   "contentLinks", "publicationDate", "lastCheckedAt"))

    url_candidates: dict[str, list] = defaultdict(list)
    for rid, urls in report_urls.items():
        for u in set(urls):
            url_candidates[u].append(rid)

    resolved: dict = {}          # url -> payload that HAS crawl data
    fallback: dict = {}          # url -> first category-only payload (no crawl)
    attempt: dict = defaultdict(int)
    lookups_done = 0

    for _pass in range(max_reports_per_url):
        todo: dict[str, list] = defaultdict(list)
        for u, cands in url_candidates.items():
            if u in resolved:
                continue
            idx = attempt[u]
            if idx < len(cands):
                todo[cands[idx]].append(u)
                attempt[u] = idx + 1
        if not todo:
            break

        async def _one(rid, urls):
            return rid, urls, await _enrich_report_batch(
                domain_id, rid, urls, topic_id=report_topic.get(rid))

        for rid, urls, data in await asyncio.gather(*[_one(rid, us) for rid, us in todo.items()]):
            lookups_done += (len(urls) + 99) // 100
            for u, payload in data.items():
                if _has_crawl(payload):
                    resolved[u] = payload
                elif u not in fallback:
                    fallback[u] = payload

    final_payload = {u: (resolved.get(u) or fallback.get(u) or {}) for u in url_candidates}

    # ─── Aggregate per URL ───
    url_to_agg: dict = defaultdict(lambda: {
        "has_own": False, "has_comp": False,
        # per-brand MAX page count (not summed across reports — the page is the
        # same regardless of how many reports cite it, so we must not multiply).
        "own_brand_counts": defaultdict(int), "comp_brand_counts": defaultdict(int),
        "categories": set(),
        "couples_enriched": 0, "couples_total": 0,
        "has_crawl": False,
    })
    for u, cands in url_candidates.items():
        agg = url_to_agg[u]
        agg["couples_total"] = len(cands)
        data = final_payload.get(u) or {}
        agg["couples_enriched"] = 1 if data else 0
        if _has_crawl(data):
            agg["has_crawl"] = True
        if data.get("sourceCategory"):
            agg["categories"].add(data["sourceCategory"])
        for b in (data.get("detectedBrands") or []):
            cnt = b.get("count", 0)
            if cnt <= 0:
                continue
            name = b.get("name") or "?"
            if b.get("isBrand"):
                agg["has_own"] = True
                agg["own_brand_counts"][name] = max(agg["own_brand_counts"][name], cnt)
            else:
                agg["has_comp"] = True
                agg["comp_brand_counts"][name] = max(agg["comp_brand_counts"][name], cnt)

    classified = []
    matrix: dict = defaultdict(lambda: defaultdict(int))
    summary: Counter = Counter()
    for u, agg in url_to_agg.items():
        ownership = "owned" if is_owned_domain(u, owned_patterns) else "external"
        st = _source_content_status(agg)
        matrix[ownership][st] += 1
        summary[st] += 1
        classified.append({
            "url": u,
            "domain": domain_from_url(u) or "",
            "ownership": ownership,
            "source_content_brand_status": st,
            "own_brands": sorted(agg["own_brand_counts"]),
            "comp_brands": sorted(agg["comp_brand_counts"]),
            "own_brand_counts": dict(agg["own_brand_counts"]),
            "comp_brand_counts": dict(agg["comp_brand_counts"]),
            "brand_mention_count": sum(agg["own_brand_counts"].values()),
            "competitor_mention_count": sum(agg["comp_brand_counts"].values()),
            "citations": url_cit.get(u),  # None in explicit mode
            "category": " | ".join(sorted(agg["categories"])) or None,
            "enriched": f"{agg['couples_enriched']}/{agg['couples_total']}",
        })

    STATUS_ORDER = {"own_only": 0, "own+comp": 1, "comp_only": 2, "no_brand": 3, "not_enriched": 4}
    classified.sort(key=lambda r: (
        STATUS_ORDER.get(r["source_content_brand_status"], 99),
        -r["brand_mention_count"],
        -(r["citations"] or 0),
    ))

    # ─── KEY OUTPUT: external sources ranked by how much they cite YOUR brand ───
    brand_citation_ranking = sorted(
        (c for c in classified if c["ownership"] == "external" and c["brand_mention_count"] > 0),
        key=lambda c: (-c["brand_mention_count"], -(c["citations"] or 0)),
    )

    statuses = ["own_only", "own+comp", "comp_only", "no_brand", "not_enriched"]
    matrix_rows = []
    for own in ("owned", "external"):
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

    # ─── Flat table: one row per URL (brand mentioned | competitors | category) ───
    def _fmt_counts(d: dict) -> str:
        """'IBIS (8), Campanile (2)' — names with page mention counts, biggest first."""
        return ", ".join(f"{n} ({c})" for n, c in sorted(d.items(), key=lambda kv: -kv[1]))

    def _short_category(cat, depth: int = 2) -> str:
        """Trim the long DataForSEO path to its top levels for readable display."""
        if not cat:
            return ""
        first = cat.split(" | ")[0]
        return " > ".join(p.strip() for p in first.split(">")[:depth])

    table = [
        {
            "url": c["url"],
            "brand": brand_name,                               # tracked brand of the topic
            "brand_mentioned": _fmt_counts(c["own_brand_counts"]),    # e.g. "IBIS (8)"
            "competitors_cited": _fmt_counts(c["comp_brand_counts"]),
            "category": c["category"],
        }
        for c in classified
    ]

    md = ["| URL | Brand mentioned | Competitors cited | Category |",
          "|-----|-----------------|-------------------|----------|"]
    for c in classified:
        bm = _fmt_counts(c["own_brand_counts"]) or "—"
        comp = _fmt_counts(c["comp_brand_counts"]) or "—"
        cat = _short_category(c["category"]) or "—"
        u = c["url"] if len(c["url"]) <= 70 else c["url"][:67] + "…"
        md.append(f"| {u} | {bm} | {comp} | {cat} |")
    markdown_table = "\n".join(md)

    brands_found = summary.get("own_only", 0) + summary.get("own+comp", 0) + summary.get("comp_only", 0)
    diagnostic = None
    if brands_found == 0 and url_to_agg:
        diagnostic = (
            f"0 brand detected across {len(url_to_agg)} URL(s). Enrichment ran fine "
            f"(enrichment_lookups={lookups_done}) but the top-cited URLs are likely "
            "brandless infrastructure pages (map tiles, booking/parking, etc.). "
            "Raise top_n (e.g. 50-100) so brand-bearing sources are included, or change source_scope."
        )

    return {
        "status": "success",
        "table": table,                 # one row per URL: brand / competitors / category
        "markdown_table": markdown_table,
        "classified_sources": classified,
        "brand_citation_ranking": brand_citation_ranking,
        "matrix": matrix_rows,
        "summary": {s: summary.get(s, 0) for s in statuses},
        "metadata": {
            "mode": mode,
            "diagnostic": diagnostic,
            "brand_name": brand_name,
            "owned_patterns": owned_patterns,
            "source_scope": source_scope if mode == "auto" else "explicit",
            "responses_scanned": scanned,
            "unique_urls": len(url_to_agg),
            "crawled_urls": sum(1 for a in url_to_agg.values() if a["has_crawl"]),
            "category_only_urls": sum(1 for a in url_to_agg.values() if not a["has_crawl"]),
            "crawl_all": crawl_all,
            "couples_total": sum(len(set(v)) for v in report_urls.values()),
            "enrichment_lookups": lookups_done,
            "max_reports_per_url": max_reports_per_url,
            "filters": {"response_brand_mentioned": response_brand_mentioned},
            "note": "source_content_brand_status comes from crawling the PAGE — "
                    "distinct from response_brand_mentioned (the LLM answer).",
        },
    }


# ══════════════════════════════════════════════════════════════════
# MCP TOOL DECLARATIONS
# ══════════════════════════════════════════════════════════════════

TOOL_DEFINITIONS: list[tuple[str, str, dict, dict]] = [
    # (name, description, inputSchema, annotations)

    (
        "mint_get_domains_and_topics",
        (
            "STEP 1 — ALWAYS START HERE. Lists every domain (brand) and topic (market) you "
            "have access to, with their IDs. Every other tool needs a domainId and/or topicId, "
            "and those IDs come from THIS tool. If you don't already have the exact IDs in the "
            "conversation, call this first."
            "\n\n"
            "USE FOR: 'What brands/markets do I have?', 'List my topics', 'Show all IBIS markets', "
            "or silently to resolve a brand/market name into IDs before another tool."
            "\n\n"
            "ROUTING MAP (which source/score tool to use next):\n"
            "  - Macro snapshot of one topic (score, SoV,\n"
            "    competitors, mentions, source mix)        -> mint_get_topic_overview\n"
            "  - One brand's score history over time      -> mint_get_topic_scores\n"
            "  - Compare many markets/brands in a table    -> mint_get_scores_overview\n"
            "  - A line chart of visibility over time      -> mint_get_visibility_trend\n"
            "  - Which sites/URLs are cited (by model/time)-> mint_get_topic_sources\n"
            "  - Who is cited, owned vs external, brand\n"
            "    mentioned or not (fast, weighted)         -> mint_get_response_sources\n"
            "  - Which cited PAGES mention my brand /\n"
            "    rank external sources citing my brand     -> mint_enrich_cited_sources\n"
            "  - Which AI models exist for a topic         -> mint_get_models_by_topic"
            "\n\n"
            "Returns: domains (id+name), topics (domainId, domainName, topicId, topicName), "
            "a 'Brand > Topic' -> IDs mapping, and any errors."
        ),
        {"type": "object", "properties": {}},
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),

    (
        "mint_get_models_by_topic",
        (
            "LIST AI MODELS FOR ONE TOPIC. Returns the exact model names available for a "
            "given topic (each topic can have a different set), resolved live from the API. "
            "These names are what you pass to the 'models' parameter of mint_get_topic_scores, "
            "mint_get_topic_sources, mint_get_scores_overview, mint_get_visibility_trend or "
            "mint_get_raw_responses."
            "\n\n"
            "USE FOR: 'Which models are tracked for IBIS FR?', or right after the user accepts "
            "a per-model deep dive, to get valid model names before filtering."
            "\n\n"
            "ASSISTANT BEHAVIOR RULE:\n"
            "  By default, answer score/source questions with the GLOBAL (all-models combined) "
            "view and do NOT call this tool. At the end of such an answer, offer one short "
            "follow-up like 'Want a deep dive on a specific model?'. ONLY if the user says yes, "
            "call this tool, then re-run the relevant tool with the chosen 'models' value."
            "\n\n"
            "Returns: domainId, topicId, models (list), count, note."
        ),
        {
            "type": "object",
            "properties": {
                "domainId": {"type": "string", "description": "Domain ID (REQUIRED). From mint_get_domains_and_topics."},
                "topicId":  {"type": "string", "description": "Topic ID (REQUIRED). From mint_get_domains_and_topics."},
            },
            "required": ["domainId", "topicId"],
        },
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),

    (
        "mint_get_topic_scores",
        (
            "SCORE HISTORY FOR ONE TOPIC — day-by-day visibility scores of your brand vs its "
            "competitors, for a single topic (one brand in one market), broken down by AI model. "
            "This is the detailed single-topic view: each day, each entity, each model."
            "\n\n"
            "USE FOR: 'How did IBIS FR score vs competitors last month?', "
            "'Show the daily score curve for Novotel UK', "
            "'Compare GPT-5.1 vs Sonar Pro scores on one topic'."
            "\n\n"
            "DON'T USE FOR (pick the right tool instead):\n"
            "  - Several topics/markets at once, as a table -> mint_get_scores_overview\n"
            "  - A ready-to-plot time-series line chart     -> mint_get_visibility_trend\n"
            "  - Anything about CITED SOURCES/URLs          -> mint_get_topic_sources or mint_get_raw_responses"
            "\n\n"
            "GOLDEN RULE: if the user does NOT mention specific dates or a model, OMIT those "
            "params (defaults: last 30 days, all models). Only pass 'models' once the user asks "
            "for a specific model (get valid names from mint_get_models_by_topic)."
        ),
        {
            "type": "object",
            "properties": {
                "domainId":  {"type": "string", "description": "Domain ID (REQUIRED). From mint_get_domains_and_topics."},
                "topicId":   {"type": "string", "description": "Topic ID (REQUIRED). From mint_get_domains_and_topics."},
                "startDate": {"type": "string", "description": "Start date YYYY-MM-DD. Optional (default: 30 days ago). Omit unless the user gave a date."},
                "endDate":   {"type": "string", "description": "End date YYYY-MM-DD. Optional (default: today). Omit unless the user gave a date."},
                "models":    {"type": "string", "description": "Comma-separated model filter, NO spaces (e.g. 'gpt-5.1,sonar-pro'). Omit for all models."},
            },
            "required": ["domainId", "topicId"],
        },
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),

    (
        "mint_get_scores_overview",
        (
            "MULTI-TOPIC SUMMARY TABLE — one average visibility score per topic over a period, "
            "returned as a compact Markdown table + JSON rows. This is the cross-market / "
            "cross-brand comparison view. It resolves topics itself from brand_filter / "
            "market_filter, so you don't pass topic IDs one by one."
            "\n\n"
            "USE FOR: 'Compare all IBIS markets', 'Q1 overview across brands', "
            "'Which market performs best?', 'Average score per market this quarter'."
            "\n\n"
            "DON'T USE FOR (pick the right tool instead):\n"
            "  - Day-by-day history of ONE topic        -> mint_get_topic_scores\n"
            "  - A line chart over time                 -> mint_get_visibility_trend\n"
            "  - Cited sources/URLs                      -> mint_get_topic_sources or mint_get_raw_responses"
            "\n\n"
            "TIP: brand_filter ('IBIS') and market_filter ('FR') narrow the scope and reduce API "
            "calls; combine both to target precisely. "
            "GOLDEN RULE: omit startDate/endDate/models unless the user specified them "
            "(defaults: last 90 days, all models combined)."
        ),
        {
            "type": "object",
            "properties": {
                "brand_filter":  {"type": "string", "description": "Filter by brand name (e.g. 'IBIS', 'Mercure', 'Fairmont'). Optional."},
                "market_filter": {"type": "string", "description": "Filter by market keyword in the topic name (e.g. 'FR', 'UK', 'DE'). Optional."},
                "topic_ids":     {"type": "array", "items": {"type": "string"}, "description": "Explicit list of topicIds. Optional (overrides filters)."},
                "startDate":     {"type": "string", "description": "Start date YYYY-MM-DD. Optional (default: 90 days ago)."},
                "endDate":       {"type": "string", "description": "End date YYYY-MM-DD. Optional (default: today)."},
                "models":        {"type": "string", "description": "Comma-separated model filter. Omit for cross-model average."},
            },
            "required": [],
        },
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),

    (
        "mint_get_visibility_trend",
        (
            "TIME-SERIES FOR CHARTING — visibility scores binned by day/week/month, shaped for "
            "line charts: {series: [{name, points: [{date, score, n}]}]}. Use this specifically "
            "when the user wants to SEE a trend/curve over time. After receiving the data, render "
            "a line chart in a Claude artifact (e.g. Recharts)."
            "\n\n"
            "USE FOR: 'Weekly IBIS visibility curve since January', 'Monthly trend chart for Q1', "
            "'Plot IBIS FR vs UK over 6 months' (use aggregation='per_topic')."
            "\n\n"
            "DON'T USE FOR (pick the right tool instead):\n"
            "  - Just numbers, no chart, one topic   -> mint_get_topic_scores\n"
            "  - A comparison table across topics     -> mint_get_scores_overview\n"
            "  - Sources/URLs                          -> mint_get_topic_sources or mint_get_raw_responses"
            "\n\n"
            "KEY PARAMS: granularity='week' (default; 'day'/'month' available); "
            "aggregation='average' (single averaged series) or 'per_topic' (one series per topic "
            "for visual comparison). Default period: Jan 1st of current year to today."
        ),
        {
            "type": "object",
            "properties": {
                "brand_filter":  {"type": "string", "description": "Filter by brand name. Optional."},
                "market_filter": {"type": "string", "description": "Filter by market keyword. Optional."},
                "topic_ids":     {"type": "array", "items": {"type": "string"}, "description": "Explicit topicId list. Optional."},
                "startDate":     {"type": "string", "description": "YYYY-MM-DD. Optional (default: Jan 1st current year)."},
                "endDate":       {"type": "string", "description": "YYYY-MM-DD. Optional (default: today)."},
                "models":        {"type": "string", "description": "Comma-separated model filter. Optional."},
                "granularity":   {"type": "string", "enum": ["day", "week", "month"], "description": "Time bin size. Default: 'week'."},
                "aggregation":   {"type": "string", "enum": ["average", "per_topic"], "description": "'average' = single series; 'per_topic' = one series per topic. Default: 'average'."},
            },
            "required": [],
        },
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),

    (
        "mint_get_topic_sources",
        (
            "WHICH SITES & URLs ARE CITED (fast, by model, over time) — for ONE topic, returns "
            "the most-cited domains and URLs, broken down by AI model, plus how citations evolve "
            "over time. Uses Mint's pre-aggregated API, so it's fast. "
            "It answers 'WHO is cited and HOW OFTEN', NOT 'does the page mention my brand'."
            "\n\n"
            "USE FOR: 'Which websites are most cited for IBIS FR?', "
            "'Top cited URLs on this topic', 'Compare cited sources between GPT-5.1 and Sonar Pro', "
            "'How did citations evolve over time for this topic?'."
            "\n\n"
            "DON'T USE FOR — use mint_get_raw_responses instead — any question about WHAT THE PAGE "
            "SAYS: whether a page mentions YOUR brand, only competitors, or both; owned vs external "
            "classification; 'who replaces me when I'm not cited'. This tool cannot tell you that "
            "(no page-content enrichment)."
            "\n\n"
            "Returns: top_domains, top_urls, domains_over_time, urls_over_time, global_metrics."
        ),
        {
            "type": "object",
            "properties": {
                "domainId":  {"type": "string", "description": "Domain ID (REQUIRED). From mint_get_domains_and_topics."},
                "topicId":   {"type": "string", "description": "Topic ID (REQUIRED). From mint_get_domains_and_topics."},
                "startDate": {"type": "string", "description": "YYYY-MM-DD. Optional (default: 90 days ago)."},
                "endDate":   {"type": "string", "description": "YYYY-MM-DD. Optional (default: today)."},
                "models":    {"type": "string", "description": "Comma-separated model filter, NO spaces. Optional (omit for all models)."},
            },
            "required": ["domainId", "topicId"],
        },
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),

    (
        "mint_get_topic_overview",
        (
            "MACRO SNAPSHOT FOR ONE TOPIC — a single fast API call that returns the headline "
            "KPIs for one brand in one market: visibility score (+ variation vs previous period), "
            "share of voice, brand rank among competitors, per-model score breakdown, competitor "
            "scores, top brand mentions (share of voice by mention count), and the source mix "
            "(your owned domains vs external sources). Start here for 'how am I doing overall on "
            "this topic'."
            "\n\n"
            "It is deliberately LIGHT: it does ONE call (no per-model fan-out) and OMITS the heavy "
            "time-series and the full domain/URL lists. For those, use the detail tools it points "
            "to in 'next_step'."
            "\n\n"
            "USE FOR:\n"
            "  - 'Give me an overview / dashboard of IBIS FR'\n"
            "  - 'What's my share of voice and how do I rank vs competitors?'\n"
            "  - 'Which brands are mentioned most on this topic?' (top_mentions)\n"
            "  - 'Per-model snapshot of my score in one call' (model_breakdown)"
            "\n\n"
            "DON'T USE FOR (use the right detail tool instead):\n"
            "  - Day-by-day score curve / per-model time series -> mint_get_topic_scores\n"
            "  - Full top cited domains & URLs (and over time)   -> mint_get_topic_sources\n"
            "  - A ready-to-plot chart                           -> mint_get_visibility_trend\n"
            "  - Owned/external + page brand analysis            -> mint_get_response_sources / mint_enrich_cited_sources\n"
            "  - Compare MANY topics in a table                  -> mint_get_scores_overview"
            "\n\n"
            "GOLDEN RULE: omit startDate/endDate/models unless the user specified them "
            "(default: last 30 days, all models / GLOBAL). Returns the GLOBAL view by default; "
            "offer a per-model deep dive afterwards if useful."
        ),
        {
            "type": "object",
            "properties": {
                "domainId":  {"type": "string", "description": "Domain ID (REQUIRED). From mint_get_domains_and_topics."},
                "topicId":   {"type": "string", "description": "Topic ID (REQUIRED). From mint_get_domains_and_topics."},
                "startDate": {"type": "string", "description": "YYYY-MM-DD. Optional (default: 30 days ago)."},
                "endDate":   {"type": "string", "description": "YYYY-MM-DD. Optional (default: today)."},
                "models":    {"type": "string", "description": "Comma-separated model filter, NO spaces. Omit for the GLOBAL (all-models) view."},
                "top_n":     {"type": "integer", "description": "Max rows for top_mentions. Default: 10, max: 100."},
                "include_model_breakdown": {"type": "boolean", "description": "Include per-model score arrays for brand and competitors. Default: true."},
                "useAllModelsForCompetitors": {"type": "boolean", "description": "If true, competitor averages count missing models as 0 (across ALL models). Default: false."},
            },
            "required": ["domainId", "topicId"],
        },
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),

    (
        "mint_get_response_sources",
        (
            "FAST SOURCE OVERVIEW (no page crawling) — for ONE domain, reads the raw LLM "
            "responses and tells you WHICH sources are cited, split owned vs external and split "
            "by whether your brand was mentioned in the answer. All counts are CITATION-WEIGHTED "
            "(a URL cited 80 times counts 80, not 1), so the ranking reflects real citation force. "
            "This is the cheap, default tool for any 'who is cited / owned vs external / who shows "
            "up when I'm not mentioned' question — it does NOT call DataForSEO."
            "\n\n"
            "TWO RESPONSE-LEVEL AXES:\n"
            "  ownership                 -> owned (brand-owned domain) vs external (third party)\n"
            "  response_brand_mentioned  -> was YOUR brand named in the LLM ANSWER (true/false)"
            "\n\n"
            "USE FOR:\n"
            "  - 'Which external domains/URLs are cited most for IBIS?'\n"
            "  - 'What share of citations come from my owned sites vs external?'\n"
            "    -> read ownership_summary\n"
            "  - 'When my brand is NOT mentioned, which sources show up?'\n"
            "    -> response_brand_mentioned='false'\n"
            "  - 'Owned URLs surfacing in answers' -> ownership_filter='owned'"
            "\n\n"
            "DOES NOT ANSWER 'does the cited PAGE itself talk about my brand' — that needs page "
            "crawling. For that, take 'next_step.sources' from this tool's output (or the top "
            "external URLs) and pass them to mint_enrich_cited_sources."
            "\n\n"
            "GOLDEN RULE: omit startDate/endDate/models unless the user specified them."
        ),
        {
            "type": "object",
            "properties": {
                "domainId":       {"type": "string", "description": "Domain ID (REQUIRED). From mint_get_domains_and_topics."},
                "topic_ids":      {"type": "array", "items": {"type": "string"}, "description": "TopicId list. Optional (defaults to ALL topics of this domain)."},
                "brand_filter":   {"type": "string", "description": "Filter topics by brand name. Optional."},
                "market_filter":  {"type": "string", "description": "Filter topics by market keyword (e.g. 'FR'). Optional."},
                "startDate":      {"type": "string", "description": "YYYY-MM-DD. Optional."},
                "endDate":        {"type": "string", "description": "YYYY-MM-DD. Optional."},
                "models":         {"type": "string", "description": "Comma-separated model filter, NO spaces. Optional."},
                "latestOnly":     {"type": "boolean", "description": "If true, use only the most recent report and ignore dates. Default: false."},
                "response_brand_mentioned": {
                    "type": "string", "enum": ["true", "false", "all"],
                    "description": "RESPONSE-level filter: was your brand mentioned in the LLM answer? 'false' is the key to 'who replaces me'. Default: 'all'.",
                },
                "ownership_filter": {
                    "type": "string", "enum": ["owned", "external", "all"],
                    "description": "Keep owned, external, or all sources in the top lists. Default: 'all'.",
                },
                "top_n": {"type": "integer", "description": "Max rows per ranking (domains/URLs). Default: 30, max: 500."},
            },
            "required": ["domainId"],
        },
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),

    (
        "mint_enrich_cited_sources",
        (
            "DEEP SOURCE ENRICHMENT (crawls page content via DataForSEO) — answers 'do the cited "
            "PAGES themselves mention my brand, my competitors, both, or neither', and ranks "
            "external sources by HOW MUCH each one cites your brand. This is the slower, paid step; "
            "use it AFTER mint_get_response_sources when the question is about page CONTENT, not "
            "just who is cited."
            "\n\n"
            "KEY OUTPUT: 'brand_citation_ranking' = external sources sorted by brand_mention_count "
            "(how many times your brand appears in each page). That answers 'which external sources "
            "cite my brand the most'."
            "\n\n"
            "source_content_brand_status per URL (distinct from response_brand_mentioned):\n"
            "  'own_only'     = page mentions YOUR brand only\n"
            "  'own+comp'     = page mentions your brand AND competitors\n"
            "  'comp_only'    = page mentions competitors only\n"
            "  'no_brand'     = crawled, no brand detected\n"
            "  'not_enriched' = no crawl data available"
            "\n\n"
            "TWO INPUT MODES:\n"
            "  AUTO (default) — pass domainId + topic selection; it fetches raw responses, ranks "
            "cited URLs by citation weight, keeps top_n (external by default via source_scope), and "
            "enriches ONLY those (cheap). Tune with top_n / source_scope / response_brand_mentioned.\n"
            "  EXPLICIT — pass 'sources' = [{url, reportId, topicId?}] to enrich an exact set, e.g. "
            "the 'next_step.sources' returned by mint_get_response_sources."
            "\n\n"
            "USE FOR:\n"
            "  - 'Which external sources cite my brand the most?' -> read brand_citation_ranking\n"
            "  - 'Which pages talk only about my competitors?' -> source_scope='external', read comp_only\n"
            "  - 'Pages mentioning me AND rivals' -> read own+comp"
            "\n\n"
            "GOLDEN RULE: keep top_n modest (default 50) — every URL is a crawl. Omit "
            "startDate/endDate/models unless the user specified them."
        ),
        {
            "type": "object",
            "properties": {
                "domainId":      {"type": "string", "description": "Domain ID (REQUIRED). From mint_get_domains_and_topics."},
                "sources":       {"type": "array", "items": {"type": "object"}, "description": "EXPLICIT mode: list of {url, reportId, topicId?}. If given, AUTO params are ignored."},
                "topic_ids":     {"type": "array", "items": {"type": "string"}, "description": "AUTO mode: topicId list. Optional (defaults to ALL topics of this domain)."},
                "brand_filter":  {"type": "string", "description": "AUTO mode: filter topics by brand name. Optional."},
                "market_filter": {"type": "string", "description": "AUTO mode: filter topics by market keyword (e.g. 'FR'). Optional."},
                "startDate":     {"type": "string", "description": "AUTO mode: YYYY-MM-DD. Optional."},
                "endDate":       {"type": "string", "description": "AUTO mode: YYYY-MM-DD. Optional."},
                "models":        {"type": "string", "description": "AUTO mode: comma-separated model filter, NO spaces. Optional."},
                "latestOnly":    {"type": "boolean", "description": "AUTO mode: only the most recent report, ignore dates. Default: false."},
                "response_brand_mentioned": {
                    "type": "string", "enum": ["true", "false", "all"],
                    "description": "AUTO mode RESPONSE-level filter before ranking. Default: 'all'.",
                },
                "source_scope": {
                    "type": "string", "enum": ["external", "owned", "all"],
                    "description": "AUTO mode: which URLs to rank & enrich. Default: 'external' (the usual question).",
                },
                "brand_name": {"type": "string", "description": "Override the brand used for owned/external classification. Optional (defaults to the domain's brand)."},
                "top_n": {"type": "integer", "description": "AUTO mode: number of top cited URLs to enrich. Default: 50, max: 300. Every URL is a crawl."},
                "crawl_all": {"type": "boolean", "description": "AUTO mode: enrich ALL in-scope cited URLs (ignores top_n), batched by 100. Heavy/slow on large scopes - may exceed TOOL_TIMEOUT; for full coverage prefer a standalone run. Default: false."},
                "max_reports_per_url": {"type": "integer", "description": "AUTO mode: how many different reports to try per URL to find a stored crawl (stops at the first crawl hit). Higher = better long-tail coverage, more lookups. Default: 3, max: 50."},
            },
            "required": ["domainId"],
        },
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    ),

    # ── mint_get_raw_responses and mint_enrich_sources are intentionally NOT exposed. ──
    # mint_get_raw_responses (the old monolithic tool) is superseded by the pair
    # mint_get_response_sources (fast) + mint_enrich_cited_sources (deep). Its handler
    # remains registered as a backward-compat alias so existing clients keep working.
    # mint_enrich_sources (direct batch enrichment) likewise stays available in code only.
]


# ══════════════════════════════════════════════════════════════════
# MCP REGISTRATION — list_tools + call_tool
# ══════════════════════════════════════════════════════════════════

_TOOL_HANDLERS: dict[str, Any] = {
    # v4.1.0 prefixed names
    "mint_get_domains_and_topics": _tool_get_domains_and_topics,
    "mint_get_models_by_topic":    _tool_get_models_by_topic,
    "mint_get_topic_scores":       _tool_get_topic_scores,
    "mint_get_scores_overview":    _tool_get_scores_overview,
    "mint_get_visibility_trend":   _tool_get_visibility_trend,
    "mint_get_topic_sources":      _tool_get_topic_sources,
    "mint_get_topic_overview":     _tool_get_topic_overview,
    # v5 source tools (split from the old monolithic raw_responses)
    "mint_get_response_sources":   _tool_get_response_sources,
    "mint_enrich_cited_sources":   _tool_enrich_cited_sources,
    # superseded / unlisted but kept callable for backward compatibility
    "mint_get_raw_responses":      _tool_get_raw_responses,
    "mint_enrich_sources":         _tool_enrich_sources,
    # v4.0.0 backward-compat aliases (same handlers)
    "get_domains_and_topics": _tool_get_domains_and_topics,
    "get_models_by_topic":    _tool_get_models_by_topic,
    "get_topic_scores":       _tool_get_topic_scores,
    "get_scores_overview":    _tool_get_scores_overview,
    "get_visibility_trend":   _tool_get_visibility_trend,
    "get_topic_sources":      _tool_get_topic_sources,
    "get_topic_overview":     _tool_get_topic_overview,
    "get_raw_responses":      _tool_get_raw_responses,
    "enrich_sources":         _tool_enrich_sources,
}


@server.list_tools()
async def list_tools() -> list[Tool]:
    """Declare the 9 exposed tools with descriptions, schemas, and annotations."""
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
        result = await asyncio.wait_for(fn(arguments or {}), timeout=TOOL_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("Tool %s timed out after %.0fs", name, TOOL_TIMEOUT)
        result = {
            "status": "error",
            "error_type": "timeout",
            "message": (
                f"This request took longer than {TOOL_TIMEOUT:.0f}s and was stopped before "
                "completing. The scope is likely too large. Try one of: narrow the date range "
                "(startDate/endDate), analyze fewer topics at once (use topic_ids or "
                "brand_filter/market_filter), filter to a single model (models=...), or for "
                "mint_get_raw_responses use aggregate='sources' which skips the slower enrichment."
            ),
        }
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