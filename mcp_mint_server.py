"""
Mint.ai Visibility MCP Server - Version 4.0.0 (Sources Classification & Enrichment)

Serveur MCP (Model Context Protocol) permettant d'accéder aux données de visibilité
de marques via l'API Mint.ai. Compatible avec les clients MCP standards (Claude Desktop)
et les clients Web utilisant le transport SSE (Server-Sent Events).

Fonctionnalités principales (v4.0.0 — 7 tools):
- get_domains_and_topics        : discovery du catalogue brands/marchés
- get_topic_scores              : scores Brand vs Competitors, 1 topic, breakdown modèle
- get_scores_overview           : vue synthétique multi-topics (tableau Markdown)
- get_visibility_trend          : série temporelle binnée (jour/semaine/mois) pour graphiques
- get_topic_sources             : top domaines/URLs cités, 1 topic
- get_raw_responses             : analyse fine des sources avec classification 2 axes (cœur v4)
- enrich_sources                : enrichissement brut batch d'URLs (catégorie + brands détectées)

Nouveautés v4.0.0:
- NOUVEAU : classification 2 axes INDÉPENDANTS pour chaque URL
  * Axe 1 (ownership)    : "owned" / "external" via regex sur domaine (all.accor.com etc.)
  * Axe 2 (brand_status) : own_only / own+comp / comp_only / no_brand / not_enriched via crawl
- NOUVEAU : get_visibility_trend pour produire des courbes temporelles binnées
- NOUVEAU : get_raw_responses avec 3 modes (classified / sources / none)
- NOUVEAU : enrich_sources exposé pour accès direct à /sources/enrichment
- FIX     : couplage (reportId, url) respecté strictement pour l'enrichment (bug v3.x)
- NOUVEAU : fichier owned_domains.json externe pour mapping brand → domaines propriétaires
- NOUVEAU : erreurs typées (AuthError, NotFoundError, RateLimitError, MintAPIError)
- NOUVEAU : retry exponentiel automatique sur 429/5xx
- NOUVEAU : semaphore globale pour limiter la concurrence API

Les 3 anciens tools v3.6 sont REMPLACÉS par 7 tools plus spécialisés :
    v3.6 get_visibility_scores          → v4 get_topic_scores
    v3.6 get_citations                  → v4 get_topic_sources + get_raw_responses
    v3.6 get_visibility_monthly_summary → v4 get_scores_overview

Variables d'environnement:
- MINT_API_KEY         : Clé d'authentification API Mint.ai (REQUIS)
- MINT_BASE_URL        : URL de base API (défaut: https://api.getmint.ai/api)
- HTTP_TIMEOUT         : Timeout HTTP en secondes (défaut: 30.0)
- HTTP_MAX_CONCURRENT  : Concurrence max requêtes API (défaut: 8)
- OWNED_DOMAINS_PATH   : Chemin vers owned_domains.json (défaut: ./owned_domains.json)
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

# Imports Starlette & Web
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


# ══════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════

MINT_API_KEY = os.getenv("MINT_API_KEY", "")
MINT_BASE_URL = os.getenv("MINT_BASE_URL", "https://api.getmint.ai/api")
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30.0"))
HTTP_MAX_CONCURRENT = int(os.getenv("HTTP_MAX_CONCURRENT", "8"))

# Chemin du fichier de mapping brand → domaines propriétaires
_OWNED_DEFAULT_PATH = Path(__file__).resolve().parent / "owned_domains.json"
OWNED_DOMAINS_PATH = os.getenv("OWNED_DOMAINS_PATH", str(_OWNED_DEFAULT_PATH))

# Logging (niveau INFO pour production)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

if not MINT_API_KEY:
    logger.warning("MINT_API_KEY environment variable is missing!")

# Semaphore globale pour limiter la concurrence des requêtes API
_HTTP_SEMAPHORE = asyncio.Semaphore(HTTP_MAX_CONCURRENT)

# Version
__version__ = "4.0.0"

# Création de l'instance du serveur MCP
server = Server("mint-visibility-mcp")


# ══════════════════════════════════════════════════════════════════
# CHARGEMENT DU MAPPING "OWNED DOMAINS"
# ══════════════════════════════════════════════════════════════════
# Le fichier owned_domains.json mappe chaque brand (nom Mint) à ses
# domaines propriétaires. Utilisé pour classer une URL comme "owned"
# (propriété de la marque) vs "external".
#
# Pourquoi ? L'API Mint ne crawle que les pages owned + concurrents.
# Pour une URL all.accor.com sans mention textuelle détectée, elle est
# quand même la propriété éditoriale de la brand → signal "owned" via
# regex sur le domaine, indépendant du crawl.

def _load_owned_domains_map() -> dict:
    """Charge le mapping depuis owned_domains.json. Fallback silencieux si absent."""
    path = Path(OWNED_DOMAINS_PATH)
    if not path.exists():
        logger.warning(
            "owned_domains.json introuvable à %s — toutes les URLs seront classées 'external'",
            path,
        )
        return {"_default": []}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        logger.error("owned_domains.json invalide (%s) — fallback sur {}", e)
        return {"_default": []}

    # On ignore les clés _comment mais on garde _default
    cleaned = {k: v for k, v in raw.items() if not k.startswith("_") or k == "_default"}
    nb = len([k for k in cleaned if k != "_default"])
    logger.info("owned_domains.json chargé — %d brand(s) configurée(s) + _default", nb)
    return cleaned


OWNED_DOMAINS_MAP = _load_owned_domains_map()


def get_owned_patterns(brand_name: str) -> list:
    """Retourne les domaines owned pour une brand, fallback sur _default."""
    return OWNED_DOMAINS_MAP.get(brand_name, OWNED_DOMAINS_MAP.get("_default", []))


# ══════════════════════════════════════════════════════════════════
# HELPERS URL & DATES
# ══════════════════════════════════════════════════════════════════

def domain_from_url(url: str) -> str | None:
    """Extrait le domaine d'une URL, strip 'www.'. Retourne None si invalide."""
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
    """
    True si l'URL appartient à un domaine owned (match exact OU sous-domaine).

    Ex avec owned_patterns=["accor.com"] :
        https://accor.com/about       → True
        https://all.accor.com/hotel   → True (sous-domaine)
        https://booking.com           → False
        https://notaccor.com          → False (pas un vrai sous-domaine)
    """
    d = domain_from_url(url)
    if not d:
        return False
    return any(d == p or d.endswith("." + p) for p in owned_patterns)


def default_date_range(days: int) -> tuple:
    """Retourne (startDate, endDate) en YYYY-MM-DD, endDate=aujourd'hui."""
    end = date.today()
    start = end - timedelta(days=days)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def week_start(iso_date: str) -> str:
    """Retourne le lundi de la semaine contenant iso_date."""
    d = datetime.fromisoformat(iso_date[:10]).date()
    return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")


def month_start(iso_date: str) -> str:
    """Retourne le 1er du mois de iso_date."""
    return datetime.fromisoformat(iso_date[:10]).date().strftime("%Y-%m-01")


def day_iso(iso_date: str) -> str:
    return iso_date[:10]


BIN_FUNCTIONS = {"day": day_iso, "week": week_start, "month": month_start}


# ══════════════════════════════════════════════════════════════════
# CLIENT HTTP — ERREURS TYPÉES + RETRY EXPONENTIEL
# ══════════════════════════════════════════════════════════════════

class MintAPIError(Exception):
    """Erreur générique API Mint."""
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class AuthError(MintAPIError):
    """401 — clé API invalide."""


class NotFoundError(MintAPIError):
    """404 — ressource introuvable."""


class RateLimitError(MintAPIError):
    """429 — rate limit dépassé."""


def _map_http_error(e: httpx.HTTPStatusError) -> MintAPIError:
    sc = e.response.status_code
    try:
        body = e.response.json()
        msg = body.get("message") or body.get("error") or str(body)
    except Exception:
        msg = e.response.text[:200]

    if sc == 401:
        return AuthError(f"Clé API invalide ou manquante : {msg}", sc)
    if sc == 404:
        return NotFoundError(f"Ressource introuvable : {msg}", sc)
    if sc == 429:
        return RateLimitError(f"Rate limit dépassé : {msg}", sc)
    return MintAPIError(f"HTTP {sc} : {msg}", sc)


async def _http_request(method: str, path: str, *,
                        params: dict | None = None,
                        json_body: dict | None = None,
                        max_retries: int = 3) -> Any:
    """Requête HTTP avec semaphore + retry exponentiel sur 429/5xx."""
    if not MINT_API_KEY:
        raise RuntimeError("MINT_API_KEY environment variable is required")

    url = f"{MINT_BASE_URL}{path}"
    backoff = 1.0

    for attempt in range(max_retries):
        async with _HTTP_SEMAPHORE:
            try:
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
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
                    logger.warning("HTTP %d — retry dans %.1fs (tentative %d/%d)",
                                   sc, wait, attempt + 1, max_retries)
                    await asyncio.sleep(wait)
                    backoff *= 2
                    continue
                raise _map_http_error(e) from e
            except httpx.RequestError as e:
                if attempt < max_retries - 1:
                    logger.warning("Network error — retry dans %.1fs : %s", backoff, e)
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise MintAPIError(f"Network error : {e}") from e

    raise MintAPIError("Max retries exceeded")


async def fetch_get(path: str, params: dict | None = None) -> Any:
    """GET avec retry + erreurs typées."""
    return await _http_request("GET", path, params=params or {})


async def fetch_post(path: str, body: dict) -> Any:
    """POST avec retry + erreurs typées."""
    return await _http_request("POST", path, json_body=body)


# ══════════════════════════════════════════════════════════════════
# HELPER : résolution catalogue + filtres
# ══════════════════════════════════════════════════════════════════

def filter_topics(all_topics: list,
                  topic_ids: list | None = None,
                  brand_filter: str | None = None,
                  market_filter: str | None = None) -> list:
    """Filtre une liste de topics selon les critères fournis."""
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
# TOOL 1/7 : get_domains_and_topics — DISCOVERY
# ══════════════════════════════════════════════════════════════════

async def get_domains_and_topics() -> dict:
    """
    Récupère tous les domaines et leurs topics disponibles.

    Retour :
    {
      "domains": [...],
      "topics":  [{"domainId", "domainName", "topicId", "topicName"}, ...],
      "mapping": {"IBIS > IBIS FR": {"domainId", "topicId"}},
      "errors":  [{"domainId", "domainName", "error"}]  # topics non récupérables
    }
    """
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
# TOOL 2/7 : get_topic_scores — SCORES DÉTAILLÉS 1 TOPIC
# ══════════════════════════════════════════════════════════════════

async def get_topic_scores(domainId: str,
                           topicId: str,
                           startDate: str | None = None,
                           endDate: str | None = None,
                           models: str | None = None) -> dict:
    """
    Dataset détaillé Brand vs Competitors pour 1 topic, breakdown par modèle.

    Appel GLOBAL + 1 appel par modèle en parallèle.
    Dataset tabulaire : {Date, EntityName, EntityType, Score, Model}.
    """
    if not startDate or not endDate:
        startDate, endDate = default_date_range(days=30)

    base_params = {
        "startDate": startDate, "endDate": endDate,
        "latestOnly": "false", "page": 1, "limit": 1000,
    }
    endpoint = f"/domains/{domainId}/topics/{topicId}/visibility/aggregated"

    global_data = await fetch_get(endpoint, base_params)
    available = global_data.get("availableModels", [])

    if models:
        requested = {m.strip() for m in models.split(",")}
        available = [m for m in available if m in requested]

    async def fetch_model(m):
        try:
            return m, await fetch_get(endpoint, {**base_params, "models": m})
        except Exception as e:
            logger.warning("Model %s failed: %s", m, e)
            return m, None

    results = await asyncio.gather(*[fetch_model(m) for m in available])
    by_model = {m: d for m, d in results if d is not None}

    dataset = []

    def add_rows(data, model_name):
        for entry in data.get("chartData", []):
            d = entry.get("date")
            dataset.append({
                "Date": d, "EntityName": "Brand", "EntityType": "Brand",
                "Score": entry.get("brand"), "Model": model_name,
            })
            for c_name, c_score in (entry.get("competitors") or {}).items():
                dataset.append({
                    "Date": d, "EntityName": c_name, "EntityType": "Competitor",
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
                "startDate": startDate, "endDate": endDate,
                "topicId": topicId, "domainId": domainId,
            },
        },
    }


# ══════════════════════════════════════════════════════════════════
# TOOL 3/7 : get_scores_overview — VUE SYNTHÉTIQUE MULTI-TOPICS
# ══════════════════════════════════════════════════════════════════

async def get_scores_overview(brand_filter: str | None = None,
                              market_filter: str | None = None,
                              topic_ids: list | None = None,
                              startDate: str | None = None,
                              endDate: str | None = None,
                              models: str | None = None) -> dict:
    """
    Score moyen de visibilité pour plusieurs topics en un seul appel.

    - Autonome : récupère lui-même les topics via filtres.
    - 1 call API par topic, parallélisé via semaphore.
    - Retourne tableau Markdown + rows JSON.
    """
    if not startDate or not endDate:
        startDate, endDate = default_date_range(days=90)

    catalog = await get_domains_and_topics()
    topics = filter_topics(catalog["topics"], topic_ids, brand_filter, market_filter)

    if not topics:
        return {
            "status": "error",
            "message": f"Aucun topic trouvé (brand='{brand_filter}', market='{market_filter}', topic_ids={topic_ids})",
        }

    params = {"limit": 100, "startDate": startDate, "endDate": endDate}
    if models:
        params["models"] = models

    async def fetch_one(t):
        try:
            d = await fetch_get(
                f"/domains/{t['domainId']}/topics/{t['topicId']}/visibility",
                params,
            )
            scores = [
                float(r["averageScore"])
                for r in d.get("reports", [])
                if r.get("averageScore") is not None
            ]
            avg = round(sum(scores) / len(scores), 1) if scores else None
            return {
                "brand": t["domainName"], "topic": t["topicName"],
                "avg_score": avg, "data_points": len(scores), "error": None,
            }
        except Exception as e:
            return {
                "brand": t["domainName"], "topic": t["topicName"],
                "avg_score": None, "data_points": 0, "error": str(e)[:100],
            }

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
    if market_filter: filter_info += f" | marché: {market_filter}"
    if models:        filter_info += f" | modèles: {models}"

    lines = [
        f"## 📊 Scores moyens — {startDate} → {endDate}",
        f"*{len(rows)} topics{filter_info}*",
        "",
        "| Brand | Topic | Score moy. | N reports | Statut |",
        "|-------|-------|:----------:|:---------:|--------|",
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
            f"**Moyenne :** {gavg} | **Meilleur :** {best['topic']} ({best['avg_score']}) | **Plus bas :** {worst['topic']} ({worst['avg_score']})",
            "_🟢 ≥60 | 🟡 40–59 | 🟠 20–39 | 🔴 <20 | ⚠️ no data_",
        ]

    return {
        "status": "success",
        "markdown_table": "\n".join(lines),
        "rows": rows,
        "metadata": {
            "startDate": startDate, "endDate": endDate,
            "models": models or "all (cross-models)",
            "topic_count": len(rows),
        },
    }


# ══════════════════════════════════════════════════════════════════
# TOOL 4/7 : get_visibility_trend — COURBE TEMPORELLE POUR GRAPHIQUES
# ══════════════════════════════════════════════════════════════════

async def get_visibility_trend(brand_filter: str | None = None,
                               market_filter: str | None = None,
                               topic_ids: list | None = None,
                               startDate: str | None = None,
                               endDate: str | None = None,
                               models: str | None = None,
                               granularity: str = "week",
                               aggregation: str = "average") -> dict:
    """
    Série temporelle des scores, binnée côté serveur (day/week/month).

    Retour prêt à plotter en line chart :
    {
      "series": [{"name": "...", "points": [{"date", "score", "n"}]}],
      "chart_hint": "line"
    }
    """
    if granularity not in BIN_FUNCTIONS:
        return {"status": "error",
                "message": f"granularity invalide : '{granularity}'. Attendu : day, week, month."}
    if aggregation not in ("average", "per_topic"):
        return {"status": "error",
                "message": f"aggregation invalide : '{aggregation}'. Attendu : average, per_topic."}

    if not startDate or not endDate:
        endDate = date.today().strftime("%Y-%m-%d")
        startDate = date(date.today().year, 1, 1).strftime("%Y-%m-%d")

    catalog = await get_domains_and_topics()
    topics = filter_topics(catalog["topics"], topic_ids, brand_filter, market_filter)

    if not topics:
        return {"status": "error",
                "message": f"Aucun topic trouvé (brand='{brand_filter}', market='{market_filter}')"}

    params = {"limit": 1000, "startDate": startDate, "endDate": endDate}
    if models:
        params["models"] = models

    async def fetch_topic_points(t):
        try:
            d = await fetch_get(
                f"/domains/{t['domainId']}/topics/{t['topicId']}/visibility",
                params,
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
            buckets = defaultdict(list)
            for p in pts:
                buckets[bin_fn(p["date"])].append(p["score"])
            points = [
                {"date": k, "score": round(sum(v) / len(v), 1), "n": len(v)}
                for k, v in sorted(buckets.items())
            ]
            series.append({"name": f"{t['domainName']} > {t['topicName']}", "points": points})
    else:  # average
        buckets = defaultdict(list)
        for _, pts in results:
            for p in pts:
                buckets[bin_fn(p["date"])].append(p["score"])
        points = [
            {"date": k, "score": round(sum(v) / len(v), 1), "n": len(v)}
            for k, v in sorted(buckets.items())
        ]
        label = f"{brand_filter or 'All'}{' / ' + market_filter if market_filter else ''} ({granularity} avg)"
        series = [{"name": label, "points": points}]

    return {
        "status": "success",
        "series": series,
        "metadata": {
            "granularity": granularity, "aggregation": aggregation,
            "startDate": startDate, "endDate": endDate,
            "topics_n": len(topics), "models": models or "all",
        },
        "chart_hint": "line",
    }


# ══════════════════════════════════════════════════════════════════
# TOOL 5/7 : get_topic_sources — TOP DOMAINES CITÉS 1 TOPIC
# ══════════════════════════════════════════════════════════════════

async def get_topic_sources(domainId: str,
                            topicId: str,
                            startDate: str | None = None,
                            endDate: str | None = None,
                            models: str | None = None) -> dict:
    """
    Top domaines, URLs et évolutions temporelles pour 1 topic, par modèle.

    Utilise /visibility/aggregated?includeDetailedResults=true (agrégation côté Mint).
    Appel GLOBAL + 1 appel par modèle en parallèle.
    """
    if not startDate or not endDate:
        startDate, endDate = default_date_range(days=90)

    base_params = {
        "startDate": startDate, "endDate": endDate,
        "includeDetailedResults": "true", "latestOnly": "false",
        "page": 1, "limit": 1000,
    }
    endpoint = f"/domains/{domainId}/topics/{topicId}/visibility/aggregated"

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

    top_domains, top_urls = [], []
    domains_over_time, urls_over_time = [], []
    metrics = []

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
                "startDate": startDate, "endDate": endDate,
                "topicId": topicId, "domainId": domainId,
            },
        },
    }


# ══════════════════════════════════════════════════════════════════
# TOOL 6/7 : get_raw_responses — CŒUR v4 (CLASSIFICATION 2 AXES)
# ══════════════════════════════════════════════════════════════════
#
# 2 axes INDÉPENDANTS pour chaque URL :
#   Axe 1 : ownership    = "owned" | "external"     (regex sur domaine)
#   Axe 2 : brand_status = "own_only" | "own+comp" | "comp_only" | "no_brand" | "not_enriched"
#                          (via API /sources/enrichment, detectedBrands)
#
# Couplage (reportId, url) respecté strictement : chaque URL est enrichie
# avec SON reportId propre (pas de dédup cross-report), conformément au
# contract de l'API Mint.

async def _fetch_raw_one_topic(domainId, topicId, params):
    """Pagination complète des raw-results pour 1 topic."""
    p = dict(params)
    p["page"] = 1
    out = []
    MAX_PAGES = 50
    while True:
        resp = await fetch_get(
            f"/domains/{domainId}/topics/{topicId}/visibility/raw-results", p,
        )
        out.extend(resp.get("results") or [])
        pg = resp.get("pagination") or {}
        if p["page"] >= pg.get("totalPages", 1):
            break
        p["page"] += 1
        if p["page"] > MAX_PAGES:
            logger.warning("Raw results pagination cut at %d pages", MAX_PAGES)
            break
    return out


async def _enrich_report_batch(domainId, reportId, urls, topicId=None):
    """Enrichit un batch d'URLs pour UN reportId. Chunks de 100 (limite API)."""
    result = {}
    for i in range(0, len(urls), 100):
        chunk = urls[i:i + 100]
        body = {"urls": chunk, "reportId": reportId}
        if topicId:
            body["topicId"] = topicId
        try:
            resp = await fetch_post(f"/domains/{domainId}/sources/enrichment", body)
            result.update(resp)
        except Exception as e:
            logger.warning("Enrich chunk failed (reportId=%s, %d urls): %s",
                           reportId, len(chunk), e)
    return result


def _classify_url(url: str, agg: dict, owned_patterns: list) -> tuple:
    """
    Retourne (ownership, brand_status).

    brand_status :
        - not_enriched → aucun couple enrichi
        - own_only     → crawl détecte ta marque, pas de concurrent
        - own+comp     → crawl détecte les deux
        - comp_only    → crawl détecte uniquement des concurrents
        - no_brand     → crawl a tourné mais rien détecté
    """
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


async def get_raw_responses(domainId: str,
                            topic_ids: list | None = None,
                            brand_filter: str | None = None,
                            market_filter: str | None = None,
                            startDate: str | None = None,
                            endDate: str | None = None,
                            models: str | None = None,
                            latestOnly: bool = False,
                            response_brand_mentioned: str = "all",
                            aggregate: str = "classified",
                            ownership_filter: str = "all",
                            brand_status_filter=None,
                            top_n: int = 30) -> dict:
    """
    Analyse fine des sources citées. 3 modes :
    - aggregate="classified" (défaut) : enrichment + classification 2 axes complète
    - aggregate="sources" : top domaines/URLs sans enrichment (plus rapide)
    - aggregate="none" : responses brutes pour drill-down
    """
    # Validation
    if response_brand_mentioned not in ("true", "false", "all"):
        return {"status": "error",
                "message": f"response_brand_mentioned invalide: '{response_brand_mentioned}'"}
    if aggregate not in ("classified", "sources", "none"):
        return {"status": "error", "message": f"aggregate invalide: '{aggregate}'"}
    if ownership_filter not in ("owned", "external", "all"):
        return {"status": "error", "message": f"ownership_filter invalide: '{ownership_filter}'"}

    valid_statuses = {"own_only", "own+comp", "comp_only", "no_brand", "not_enriched"}
    if brand_status_filter is None or brand_status_filter == "all":
        wanted_statuses = valid_statuses
    elif isinstance(brand_status_filter, str):
        wanted_statuses = {brand_status_filter}
    else:
        wanted_statuses = set(brand_status_filter)
    unknown = wanted_statuses - valid_statuses
    if unknown:
        return {"status": "error",
                "message": f"brand_status_filter invalide: {unknown}"}

    # Résolution topics
    catalog = await get_domains_and_topics()
    topics = filter_topics(catalog["topics"], topic_ids, brand_filter, market_filter)
    topics = [t for t in topics if t["domainId"] == domainId]
    if not topics:
        return {
            "status": "error",
            "message": f"Aucun topic trouvé pour domainId={domainId}",
        }

    brand_name = topics[0]["domainName"]
    owned_patterns = get_owned_patterns(brand_name)

    # Fetch raw results en parallèle sur les topics
    params = {"limit": 100}
    if startDate:  params["startDate"] = startDate
    if endDate:    params["endDate"] = endDate
    if models:     params["models"] = models
    if latestOnly: params["latestOnly"] = "true"

    async def fetch_topic(t):
        results = await _fetch_raw_one_topic(t["domainId"], t["topicId"], params)
        for r in results:
            r["_topicName"] = t["topicName"]
            r["_domainName"] = t["domainName"]
        return results

    fetched = await asyncio.gather(*[fetch_topic(t) for t in topics])
    all_raw = [r for batch in fetched for r in batch]

    # Filtre niveau réponse
    if response_brand_mentioned == "true":
        responses = [r for r in all_raw if r.get("brandMentioned") is True]
    elif response_brand_mentioned == "false":
        responses = [r for r in all_raw if r.get("brandMentioned") is False]
    else:
        responses = all_raw

    # ─── Mode "none" : retour brut ────────────────────────────
    if aggregate == "none":
        return {
            "status": "success",
            "responses": responses,
            "metadata": {
                "topics_n": len(topics),
                "raw_total": len(all_raw),
                "after_response_filter": len(responses),
                "brand_name": brand_name,
                "owned_patterns": owned_patterns,
            },
        }

    # ─── Mode "sources" : top domaines/URLs sans enrichment ───
    if aggregate == "sources":
        domain_c, url_c, tom_c = Counter(), Counter(), Counter()
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
                "topics_n": len(topics),
                "raw_total": len(all_raw),
                "after_response_filter": len(responses),
                "brand_name": brand_name,
                "filters": {
                    "response_brand_mentioned": response_brand_mentioned,
                    "models": models,
                },
            },
        }

    # ─── Mode "classified" : enrichment + classification 2 axes ───

    # Étape 1 : collecte des couples (reportId, url) SANS dédup cross-report
    report_to_urls = defaultdict(set)
    for r in responses:
        rid = r.get("reportId")
        if not rid:
            continue
        for c in (r.get("citations") or []):
            u = c.get("url")
            if u:
                report_to_urls[rid].add(u)

    # Étape 2 : enrichment par reportId en parallèle
    async def one_report(rid):
        urls = list(report_to_urls[rid])
        if not urls:
            return {}
        data = await _enrich_report_batch(domainId, rid, urls, topicId=topics[0]["topicId"])
        return {(rid, url): payload for url, payload in data.items()}

    enrich_results = await asyncio.gather(*[one_report(rid) for rid in report_to_urls.keys()])
    enriched_by_couple = {}
    for batch in enrich_results:
        enriched_by_couple.update(batch)

    # Étape 3 : agrégation par URL (une URL dans N reports → "own" si AU MOINS UN détecte)
    url_to_aggregated = defaultdict(lambda: {
        "has_own": False, "has_comp": False,
        "own_brands": set(), "comp_brands": set(),
        "own_count_total": 0, "comp_count_total": 0,
        "categories": set(),
        "couples_enriched": 0, "couples_total": 0,
    })

    for rid, urls_set in report_to_urls.items():
        for url in urls_set:
            url_to_aggregated[url]["couples_total"] += 1

    for (rid, url), data in enriched_by_couple.items():
        agg = url_to_aggregated[url]
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

    # Étape 4 : classification + filtrage + matrice croisée
    all_records = []
    matrix = defaultdict(lambda: defaultdict(int))

    for url, agg in url_to_aggregated.items():
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
    for ownership in ["owned", "external"]:
        row = {"ownership": ownership}
        total = 0
        for s in statuses:
            v = matrix[ownership].get(s, 0)
            row[s] = v
            total += v
        row["TOTAL"] = total
        matrix_rows.append(row)
    total_row = {"ownership": "TOTAL"}
    for s in statuses:
        total_row[s] = sum(matrix[o].get(s, 0) for o in ("owned", "external"))
    total_row["TOTAL"] = sum(total_row[s] for s in statuses)
    matrix_rows.append(total_row)

    return {
        "status": "success",
        "classified_urls": all_records[:top_n * 10],
        "matrix": matrix_rows,
        "metadata": {
            "topics_n": len(topics),
            "raw_total": len(all_raw),
            "after_response_filter": len(responses),
            "unique_urls": len(url_to_aggregated),
            "couples_total": sum(len(s) for s in report_to_urls.values()),
            "couples_enriched": len(enriched_by_couple),
            "brand_name": brand_name,
            "owned_patterns": owned_patterns,
            "filters": {
                "response_brand_mentioned": response_brand_mentioned,
                "ownership_filter": ownership_filter,
                "brand_status_filter": sorted(wanted_statuses),
                "models": models,
            },
        },
    }


# ══════════════════════════════════════════════════════════════════
# TOOL 7/7 : enrich_sources — ACCÈS DIRECT ENRICHMENT
# ══════════════════════════════════════════════════════════════════

async def enrich_sources(domainId: str,
                         urls: list,
                         reportId: str,
                         topicId: str | None = None,
                         brand_name: str | None = None) -> dict:
    """
    Enrichit un batch d'URLs avec catégorie DataForSEO + brands détectées.

    Chunks automatiques à 100 URLs (limite API).
    Si brand_name fourni, classification owned/external ajoutée.
    """
    if not urls:
        return {"status": "error", "message": "urls vide"}
    if len(urls) > 1000:
        return {"status": "error",
                "message": f"Trop d'URLs ({len(urls)}). Max recommandé: 1000 par appel."}

    enriched = {}
    for i in range(0, len(urls), 100):
        chunk = urls[i:i + 100]
        body = {"urls": chunk, "reportId": reportId}
        if topicId:
            body["topicId"] = topicId
        try:
            resp = await fetch_post(f"/domains/{domainId}/sources/enrichment", body)
            enriched.update(resp)
        except Exception as e:
            logger.warning("Enrich chunk failed: %s", e)

    omitted = [u for u in urls if u not in enriched]
    own_hits = 0
    comp_hits = 0
    for data in enriched.values():
        for b in (data.get("detectedBrands") or []):
            if b.get("isBrand"):
                own_hits += b.get("count", 0)
            else:
                comp_hits += b.get("count", 0)

    ownership = None
    if brand_name:
        patterns = get_owned_patterns(brand_name)
        ownership = {u: ("owned" if is_owned_domain(u, patterns) else "external") for u in urls}

    return {
        "status": "success",
        "enriched": enriched,
        "omitted": omitted,
        "ownership": ownership,
        "stats": {
            "total": len(urls),
            "enriched": len(enriched),
            "omitted": len(omitted),
            "own_hits": own_hits,
            "comp_hits": comp_hits,
            "coverage_pct": round(100 * len(enriched) / max(len(urls), 1), 1),
        },
        "metadata": {"reportId": reportId, "topicId": topicId, "brand_name": brand_name},
    }


# ══════════════════════════════════════════════════════════════════
# DÉCLARATION MCP DES 7 TOOLS (DESCRIPTIONS SOIGNÉES POUR LE LLM)
# ══════════════════════════════════════════════════════════════════

@server.list_tools()
async def list_tools() -> list[Tool]:
    """
    Déclare les 7 tools avec descriptions détaillées.

    Les descriptions sont LUES PAR LE LLM pour décider quel tool appeler.
    Elles doivent donc :
    - Énoncer clairement le use case principal
    - Préciser ✅ UTILISER pour et ❌ NE PAS UTILISER pour (avec renvoi vers l'alternative)
    - Rappeler la règle d'or : omettre les params optionnels quand non mentionnés
    """
    return [
        # ─── 1/7 : DISCOVERY ────────────────────────────────────────
        Tool(
            name="get_domains_and_topics",
            description=(
                "🌍 DISCOVERY — liste tous les domaines (brands) et topics (marchés) disponibles. "
                "À appeler en PREMIER pour récupérer les IDs nécessaires aux autres tools, "
                "ou simplement pour lister les brands/marchés que l'utilisateur peut analyser. "
                "\n\n"
                "✅ UTILISER pour : "
                "'Quelles brands j'ai ?', 'Liste mes topics', 'Montre-moi tous les marchés IBIS', "
                "identifier l'ID d'un topic avant d'appeler un autre tool. "
                "\n\n"
                "📤 Retour : domains, topics, mapping \"Brand > Topic\" → IDs, errors."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),

        # ─── 2/7 : SCORES 1 TOPIC ───────────────────────────────────
        Tool(
            name="get_topic_scores",
            description=(
                "📈 SCORES DÉTAILLÉS 1 TOPIC — historique jour par jour des scores Brand vs Competitors, "
                "décomposition par modèle IA. Appel GLOBAL + 1 appel par modèle en parallèle. "
                "\n\n"
                "✅ UTILISER pour : "
                "'Évolution IBIS FR vs concurrents', 'Compare score GPT-5.1 vs Sonar Pro sur Novotel UK', "
                "zoom détaillé sur UN topic précis avec historique complet par modèle. "
                "\n\n"
                "❌ NE PAS UTILISER pour : "
                "→ vue multi-topics synthétique → get_scores_overview "
                "→ courbe/graphique temporel → get_visibility_trend "
                "→ analyser les sources citées → get_topic_sources ou get_raw_responses "
                "\n\n"
                "🤖 Modèles disponibles : GLOBAL, gpt-5.1, sonar-pro, google-ai-overview, "
                "gemini-3-pro-preview, gpt-5, gpt-interface. "
                "\n\n"
                "⚙️ RÈGLE D'OR : si l'utilisateur ne mentionne pas de dates/modèle, OMETS ces params — "
                "défaut : 30 derniers jours, tous les modèles. Ne jamais forcer startDate/endDate "
                "ou models='GLOBAL' arbitrairement."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "domainId":  {"type": "string", "description": "ID du domaine (REQUIS)"},
                    "topicId":   {"type": "string", "description": "ID du topic (REQUIS)"},
                    "startDate": {"type": "string", "description": "YYYY-MM-DD (optionnel, défaut -30j)"},
                    "endDate":   {"type": "string", "description": "YYYY-MM-DD (optionnel, défaut aujourd'hui)"},
                    "models":    {"type": "string", "description": "Filtre modèles séparés par virgule SANS espaces. Ex: 'gpt-5.1,sonar-pro' (optionnel)"},
                },
                "required": ["domainId", "topicId"],
            },
        ),

        # ─── 3/7 : SCORES N TOPICS ──────────────────────────────────
        Tool(
            name="get_scores_overview",
            description=(
                "📊 VUE SYNTHÉTIQUE MULTI-TOPICS — score moyen par topic sur une période, "
                "dans un tableau Markdown compact. Tool AUTONOME : récupère lui-même les topics "
                "via brand_filter/market_filter. "
                "\n\n"
                "✅ UTILISER pour : "
                "'Compare tous les marchés IBIS sur janvier', 'Synthèse Q1 2026 toutes brands', "
                "'Quel marché performe le mieux ?'. "
                "\n\n"
                "❌ NE PAS UTILISER pour : "
                "→ historique jour par jour → get_topic_scores "
                "→ courbe temporelle → get_visibility_trend "
                "→ analyser les sources → get_raw_responses "
                "\n\n"
                "💡 Les filtres brand_filter ('IBIS') et market_filter ('FR') réduisent le nombre de calls API. "
                "\n\n"
                "⚙️ RÈGLE D'OR : omettre startDate/endDate/models par défaut (serveur utilise 90j + tous modèles)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brand_filter":  {"type": "string", "description": "Ex: 'IBIS', 'Mercure', 'Fairmont' (optionnel)"},
                    "market_filter": {"type": "string", "description": "Ex: 'FR', 'UK', 'DE' (optionnel)"},
                    "topic_ids":     {"type": "array", "items": {"type": "string"}, "description": "Liste explicite de topicIds (optionnel)"},
                    "startDate":     {"type": "string", "description": "YYYY-MM-DD (défaut -90j)"},
                    "endDate":       {"type": "string"},
                    "models":        {"type": "string"},
                },
                "required": [],
            },
        ),

        # ─── 4/7 : TREND GRAPHIQUE ──────────────────────────────────
        Tool(
            name="get_visibility_trend",
            description=(
                "📉 COURBE TEMPORELLE BINNÉE — série temporelle des scores, agrégée par jour/semaine/mois. "
                "Retour au format prêt à plotter {series: [{name, points: [{date, score, n}]}]}. "
                "Quand tu reçois ce retour, GÉNÈRE un line chart dans un artifact Claude. "
                "\n\n"
                "✅ UTILISER pour : "
                "'Courbe hebdo de visibilité IBIS depuis janvier', "
                "'Graphique de visibilité moyenne sur Q1', "
                "'Compare l'évolution IBIS FR vs UK sur 6 mois' (aggregation='per_topic'). "
                "\n\n"
                "❌ NE PAS UTILISER si l'utilisateur veut juste des chiffres sans graphique "
                "→ get_scores_overview ou get_topic_scores. "
                "\n\n"
                "⚙️ PARAMS CLÉS : "
                "- granularity='week' (défaut) = 1 point par semaine (lundi) "
                "- aggregation='average' (défaut) = 1 seule série moyennée "
                "- aggregation='per_topic' = 1 série par topic pour comparaison visuelle "
                "\n\n"
                "Par défaut : depuis le 1er janvier de l'année en cours."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brand_filter":  {"type": "string"},
                    "market_filter": {"type": "string"},
                    "topic_ids":     {"type": "array", "items": {"type": "string"}},
                    "startDate":     {"type": "string", "description": "YYYY-MM-DD (défaut 1er janvier année courante)"},
                    "endDate":       {"type": "string"},
                    "models":        {"type": "string"},
                    "granularity":   {"type": "string", "enum": ["day", "week", "month"], "description": "Bin temporel (défaut 'week')"},
                    "aggregation":   {"type": "string", "enum": ["average", "per_topic"], "description": "'average' = 1 série moyennée, 'per_topic' = 1 par topic (défaut 'average')"},
                },
                "required": [],
            },
        ),

        # ─── 5/7 : SOURCES 1 TOPIC ──────────────────────────────────
        Tool(
            name="get_topic_sources",
            description=(
                "🔗 TOP DOMAINES/URLS CITÉS — 1 topic, breakdown par modèle. "
                "Utilise l'API aggregated qui fournit les tops déjà calculés par Mint (rapide). "
                "\n\n"
                "✅ UTILISER pour : "
                "'Quels sites sont le plus cités sur IBIS FR ?', "
                "'Compare les sources entre GPT-5.1 et Sonar Pro sur Novotel UK', "
                "'Évolution temporelle des citations pour un topic'. "
                "\n\n"
                "❌ NE PAS UTILISER pour : "
                "→ classifier les sources (qui mentionne la marque, qui mentionne les concurrents) → get_raw_responses "
                "→ enrichissement catégorie/brand detection → get_raw_responses ou enrich_sources "
                "\n\n"
                "📤 Retour : top_domains, top_urls, domains_over_time, urls_over_time, global_metrics."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "domainId":  {"type": "string", "description": "ID du domaine (REQUIS)"},
                    "topicId":   {"type": "string", "description": "ID du topic (REQUIS)"},
                    "startDate": {"type": "string", "description": "YYYY-MM-DD (défaut -90j)"},
                    "endDate":   {"type": "string"},
                    "models":    {"type": "string"},
                },
                "required": ["domainId", "topicId"],
            },
        ),

        # ─── 6/7 : RAW RESPONSES — LE TOOL CŒUR ─────────────────────
        Tool(
            name="get_raw_responses",
            description=(
                "🎯 ANALYSE FINE DES SOURCES — tool cœur pour toutes les questions de classification "
                "et de share of voice des citations. "
                "\n\n"
                "CONCEPT CLÉ : chaque URL est classée sur 2 AXES INDÉPENDANTS : "
                "\n\n"
                "**AXE 1 — ownership** (via regex sur domaine, indépendant du crawl) : "
                "• 'owned' = URL sur un domaine propriétaire (ex: all.accor.com pour IBIS) "
                "• 'external' = URL sur un domaine tiers (booking.com, tripadvisor.com...) "
                "\n\n"
                "**AXE 2 — brand_status** (via crawl /sources/enrichment) : "
                "• 'own_only'     = page mentionne ta marque uniquement "
                "• 'own+comp'     = page mentionne ta marque ET des concurrents "
                "• 'comp_only'    = page mentionne uniquement des concurrents "
                "• 'no_brand'     = crawl a tourné mais rien détecté textuellement "
                "• 'not_enriched' = pas de crawl disponible "
                "\n\n"
                "✅ USE CASES TYPES : "
                "\n"
                "1) 'Quand IBIS est cité, quelles sources mentionnent aussi des concurrents ?' → "
                "response_brand_mentioned='true' + brand_status_filter=['comp_only','own+comp'] "
                "\n"
                "2) 'Quelles sources externes mentionnent ma marque ?' → "
                "ownership_filter='external' + brand_status_filter=['own_only','own+comp'] "
                "\n"
                "3) 'Sources qui citent uniquement mes concurrents' → brand_status_filter='comp_only' "
                "\n"
                "4) 'URLs de mes sites propres qui apparaissent dans les réponses' → ownership_filter='owned' "
                "\n"
                "5) 'Quand IBIS n'est PAS cité, qui prend ma place ?' → "
                "response_brand_mentioned='false' + aggregate='sources' (plus rapide) "
                "\n\n"
                "⚙️ 3 MODES via aggregate : "
                "• 'classified' (défaut) = classification complète 2 axes + matrice croisée "
                "• 'sources' = top domaines/URLs sans enrichment (plus rapide, pour vue globale) "
                "• 'none' = raw responses brutes pour drill-down "
                "\n\n"
                "🔒 Respecte strictement le couplage (reportId, url) exigé par l'API Mint."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "domainId":       {"type": "string", "description": "ID du domaine (REQUIS)"},
                    "topic_ids":      {"type": "array", "items": {"type": "string"}, "description": "Liste topicIds (optionnel, sinon tous ceux du domaine)"},
                    "brand_filter":   {"type": "string"},
                    "market_filter":  {"type": "string"},
                    "startDate":      {"type": "string"},
                    "endDate":        {"type": "string"},
                    "models":         {"type": "string"},
                    "latestOnly":     {"type": "boolean", "description": "Si true, ignore les dates et prend uniquement le report le plus récent"},
                    "response_brand_mentioned": {
                        "type": "string", "enum": ["true", "false", "all"],
                        "description": "Filtre niveau RÉPONSE LLM : la marque est-elle mentionnée dans la réponse ? (défaut 'all')",
                    },
                    "aggregate": {
                        "type": "string", "enum": ["classified", "sources", "none"],
                        "description": "Mode de sortie (défaut 'classified')",
                    },
                    "ownership_filter": {
                        "type": "string", "enum": ["owned", "external", "all"],
                        "description": "Axe 1 — filtrer propriétaires vs externes (défaut 'all')",
                    },
                    "brand_status_filter": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                        "description": "Axe 2 — une valeur ou liste parmi : own_only, own+comp, comp_only, no_brand, not_enriched. None = tous",
                    },
                    "top_n": {"type": "integer", "description": "Nombre max d'URLs retournées (défaut 30)"},
                },
                "required": ["domainId"],
            },
        ),

        # ─── 7/7 : ENRICH SOURCES ───────────────────────────────────
        Tool(
            name="enrich_sources",
            description=(
                "🔍 ENRICHISSEMENT BATCH — pour une liste d'URLs + un reportId, "
                "récupère catégorie DataForSEO + brands détectées sur chaque page crawlée. "
                "Chunks automatiques à 100 URLs (limite API). "
                "\n\n"
                "✅ UTILISER pour : "
                "debug d'URLs spécifiques, workflows custom hors du flow raw_responses, "
                "enrichir manuellement un batch d'URLs connu. "
                "\n\n"
                "⚠️ POUR L'ANALYSE TYPIQUE (qui cite ma marque / mes concurrents), "
                "utiliser plutôt get_raw_responses(aggregate='classified') qui automatise "
                "raw → enrichment → classification. "
                "\n\n"
                "📤 Retour : enriched (map URL → {sourceCategory, detectedBrands}), omitted, stats, ownership (si brand_name fourni)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "domainId":   {"type": "string", "description": "ID du domaine (REQUIS)"},
                    "urls":       {"type": "array", "items": {"type": "string"}, "description": "URLs à enrichir (REQUIS)"},
                    "reportId":   {"type": "string", "description": "Report ID source (REQUIS — l'API indexe par (reportId, url))"},
                    "topicId":    {"type": "string", "description": "Topic ID pour résoudre market/langue (optionnel)"},
                    "brand_name": {"type": "string", "description": "Si fourni, ajoute classification owned/external"},
                },
                "required": ["domainId", "urls", "reportId"],
            },
        ),
    ]


# ══════════════════════════════════════════════════════════════════
# ROUTAGE DES APPELS — dispatch vers la bonne fonction Python
# ══════════════════════════════════════════════════════════════════

TOOL_ROUTES = {
    "get_domains_and_topics": get_domains_and_topics,
    "get_topic_scores":       get_topic_scores,
    "get_scores_overview":    get_scores_overview,
    "get_visibility_trend":   get_visibility_trend,
    "get_topic_sources":      get_topic_sources,
    "get_raw_responses":      get_raw_responses,
    "enrich_sources":         enrich_sources,
}


@server.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    """
    Dispatcher MCP : route l'appel vers la bonne fonction Python et sérialise.
    Gère les erreurs typées en les transformant en messages structurés.
    """
    fn = TOOL_ROUTES.get(name)
    if fn is None:
        return [TextContent(
            type="text",
            text=json.dumps({"status": "error", "message": f"Unknown tool: {name}"}),
        )]

    try:
        result = await fn(**(arguments or {}))
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
# CONFIGURATION WEB (TRANSPORT SSE & ROUTING)
# ══════════════════════════════════════════════════════════════════

sse = SseServerTransport("/messages")


async def handle_sse_connect(request: Request):
    """
    Gère la connexion initiale SSE (requête GET).

    Crée les streams bidirectionnels et lance la boucle principale du serveur MCP.
    Reste active pendant toute la durée de la session MCP.
    """
    async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
        await server.run(streams[0], streams[1], server.create_initialization_options())


async def handle_messages(request: Request):
    """Traite les messages JSON-RPC envoyés en POST par le client MCP."""
    await sse.handle_post_message(request.scope, request.receive, request._send)


async def handle_health(_request: Request):
    """Endpoint /health pour les healthchecks Render/Koyeb/Docker."""
    return JSONResponse({
        "status": "ok",
        "service": "mint-visibility-mcp",
        "version": __version__,
    })


# ── ROUTES HTTP ──
# Configuration critique pour compatibilité multi-clients :
# - /sse (GET)       : connexion SSE (Claude.ai, clients web)
# - /sse (POST)      : messages JSON-RPC (clients web — fix 405 v3.3.0)
# - /messages (POST) : messages JSON-RPC (clients MCP stricts — Claude Desktop)
# - /health (GET)    : healthcheck pour plateformes de déploiement
routes = [
    Route("/",         endpoint=handle_health,      methods=["GET"]),
    Route("/health",   endpoint=handle_health,      methods=["GET"]),
    Route("/sse",      endpoint=handle_sse_connect, methods=["GET"]),
    Route("/sse",      endpoint=handle_messages,    methods=["POST"]),
    Route("/messages", endpoint=handle_messages,    methods=["POST"]),
]

# CORS permissif pour développement.
# En production stricte, restreindre allow_origins à une liste explicite.
middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
]

# Création de l'application Starlette
app = Starlette(debug=False, routes=routes, middleware=middleware)
