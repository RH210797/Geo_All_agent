"""
Mint.ai Visibility MCP Server - Version 4.0.0 (Optimized Output)

Serveur MCP compatible avec les clients stricts (Claude) ET les clients Web
qui envoient les messages sur /sse (Correction erreur 405).

=== DESCRIPTION GÉNÉRALE ===
Ce serveur MCP expose deux outils pour analyser la visibilité d'une marque
dans les réponses des modèles d'IA générative (ChatGPT, Gemini, Perplexity, etc.).

Les données proviennent de l'API Mint.ai (https://getmint.ai) qui crawle les
réponses de différents LLMs pour mesurer à quelle fréquence et avec quel score
une marque (brand) est citée par rapport à ses concurrents sur un sujet donné.

=== NOMENCLATURE DES MODÈLES DISPONIBLES ===
Voici les identifiants exacts à utiliser dans le paramètre "models" :
  - "gpt-5.1"                  → OpenAI GPT-5.1
  - "gpt-5"                    → OpenAI GPT-5
  - "gpt-interface"            → ChatGPT (interface conversationnelle OpenAI)
  - "gemini-3-pro-preview"     → Google Gemini 3 Pro Preview
  - "google-ai-overview"       → Google AI Overview (résultats enrichis Google Search)
  - "sonar-pro"                → Perplexity Sonar Pro

IMPORTANT : Si l'utilisateur demande les données pour plusieurs modèles spécifiques,
il faut effectuer UN appel API séparé par modèle (un call avec models="gpt-5",
un autre avec models="gemini-3-pro-preview", etc.) car l'API n'accepte qu'un
seul modèle par requête.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import date, timedelta
from typing import Any

import httpx
from mcp.server import Server
from mcp.types import Tool, TextContent
from mcp.server.sse import SseServerTransport

# Imports Starlette & Web
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request

# Configuration
MINT_API_KEY = os.getenv("MINT_API_KEY", "")
MINT_BASE_URL = os.getenv("MINT_BASE_URL", "https://api.getmint.ai/api")

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

if not MINT_API_KEY:
    logger.warning("MINT_API_KEY environment variable is missing!")

# Création du serveur MCP
server = Server("mint-visibility-mcp")

# Liste des modèles valides (référence)
VALID_MODELS = [
    "gpt-5.1",
    "gpt-5",
    "gpt-interface",
    "gemini-3-pro-preview",
    "google-ai-overview",
    "sonar-pro",
]


# ========== LOGIQUE MÉTIER (API & TOOLS) ==========

async def fetch_api(path: str, params: dict = None) -> dict:
    """Effectue une requête GET vers l'API Mint.ai"""
    if not MINT_API_KEY:
        raise RuntimeError("MINT_API_KEY environment variable is required")
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{MINT_BASE_URL}{path}", params=params or {}, headers={"X-API-Key": MINT_API_KEY}, timeout=30.0)
        response.raise_for_status()
        return response.json()

async def get_domains_and_topics() -> dict:
    domains = await fetch_api("/domains")
    all_topics = []
    mapping = {}
    for domain in domains:
        d_id = domain.get("id")
        d_name = domain.get("displayName", domain.get("name", "Unknown"))
        try:
            topics = await fetch_api(f"/domains/{d_id}/topics")
            for topic in topics:
                t_id = topic.get("id")
                t_name = topic.get("displayName", topic.get("name", "Unknown"))
                all_topics.append({"id": t_id, "name": t_name, "domainId": d_id, "domainName": d_name})
                mapping[f"{d_name} > {t_name}"] = {"domainId": d_id, "topicId": t_id}
        except Exception: continue
    return {"status": "success", "data": {"domains": domains, "topics": all_topics, "mapping": mapping}}


async def get_visibility_scores(domainId: str, topicId: str, startDate: str = None, endDate: str = None, models: str = None) -> dict:
    """
    Récupère les scores de visibilité et retourne un dataset TABULAIRE (TSV)
    optimisé pour la consommation LLM (moins de tokens, lecture directe).

    Comportement par défaut (sans paramètres optionnels) :
      - Période : 30 derniers jours
      - Modèles : agrégation GLOBAL uniquement (tous modèles confondus)

    Si models est fourni : un appel API est fait PAR modèle demandé.
    Format models : un seul modèle par appel. Pour plusieurs modèles,
    le LLM doit faire plusieurs appels séparés.
    """
    # --- Dates par défaut : 30 derniers jours ---
    if not startDate or not endDate:
        endDate = date.today().strftime("%Y-%m-%d")
        startDate = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")

    base_params = {"startDate": startDate, "endDate": endDate, "latestOnly": "false", "page": "1", "limit": "100"}

    # --- Déterminer quels modèles requêter ---
    models_to_fetch = []
    if models:
        # L'utilisateur a demandé un modèle spécifique
        models_to_fetch = [m.strip() for m in models.split(",") if m.strip()]
    
    # --- Récupération des données ---
    # Toujours récupérer le GLOBAL (agrégé tous modèles)
    global_data = await fetch_api(f"/domains/{domainId}/topics/{topicId}/visibility/aggregated", base_params)
    
    # Récupération par modèle spécifique si demandé
    by_model_data = {}
    if models_to_fetch:
        for m in models_to_fetch:
            try:
                by_model_data[m] = await fetch_api(
                    f"/domains/{domainId}/topics/{topicId}/visibility/aggregated",
                    {**base_params, "models": m}
                )
            except Exception:
                pass
    
    # --- Construction du dataset tabulaire (TSV) ---
    # Format : Date | Model | Brand_Score | Competitor1_Name | Competitor1_Score | ... | Competitor5_Name | Competitor5_Score
    #
    # Chaque ligne = une date × un modèle
    # La Brand est toujours en premier, suivie des top 5 concurrents triés par score décroissant

    header = "Date\tModel\tBrand_Score\tCompetitor1_Name\tCompetitor1_Score\tCompetitor2_Name\tCompetitor2_Score\tCompetitor3_Name\tCompetitor3_Score\tCompetitor4_Name\tCompetitor4_Score\tCompetitor5_Name\tCompetitor5_Score"
    rows = []

    def build_rows(data: dict, model_label: str):
        for entry in data.get("chartData", []):
            d = entry.get("date", "")
            brand_score = entry.get("brand", "")
            
            # Trier les concurrents par score décroissant, garder top 5
            competitors = entry.get("competitors", {})
            sorted_competitors = sorted(competitors.items(), key=lambda x: x[1] if x[1] is not None else -1, reverse=True)[:5]
            
            # Construire les colonnes concurrents (toujours 5 colonnes, vide si < 5)
            comp_cols = []
            for i in range(5):
                if i < len(sorted_competitors):
                    comp_cols.append(f"{sorted_competitors[i][0]}\t{sorted_competitors[i][1]}")
                else:
                    comp_cols.append("\t")
            
            row = f"{d}\t{model_label}\t{brand_score}\t" + "\t".join(comp_cols)
            rows.append(row)

    # Données globales (toujours présentes)
    build_rows(global_data, "GLOBAL")

    # Données par modèle (uniquement si demandé)
    for m, data in by_model_data.items():
        build_rows(data, m)

    tsv_output = header + "\n" + "\n".join(rows)

    # --- Métadonnées compactes ---
    available_models = global_data.get("availableModels", [])
    meta_line = f"PERIOD: {startDate} to {endDate} | AVAILABLE_MODELS: {', '.join(available_models)} | ROWS: {len(rows)}"

    return {"status": "success", "metadata": meta_line, "tsv_dataset": tsv_output}


# ========== DÉCLARATION DES OUTILS MCP ==========

TOOL_GET_DOMAINS_AND_TOPICS = Tool(
    name="get_domains_and_topics",
    description="""Récupère la liste complète des domaines (marques/entreprises) et de leurs topics (sujets/thématiques) configurés dans Mint.ai.

QUAND L'UTILISER :
- Toujours en PREMIER appel avant get_visibility_scores, pour obtenir les identifiants domainId et topicId nécessaires.
- Quand l'utilisateur demande "quels sont mes domaines", "quels sujets sont suivis", etc.

CE QUE ÇA RETOURNE :
- La liste des domaines (marques suivies) avec leurs IDs.
- La liste des topics (sujets analysés) rattachés à chaque domaine, avec leurs IDs.
- Un mapping "Domaine > Topic" → {domainId, topicId} pour faciliter la recherche.

AUCUN PARAMÈTRE REQUIS. Cet outil n'a aucun paramètre d'entrée.""",
    inputSchema={
        "type": "object",
        "properties": {},
        "additionalProperties": False
    }
)

TOOL_GET_VISIBILITY_SCORES = Tool(
    name="get_visibility_scores",
    description="""Récupère les scores de visibilité IA d'une marque (brand) et de ses concurrents sur un topic donné, et retourne un dataset TABULAIRE (TSV) compact et optimisé.

QUAND L'UTILISER :
- Quand l'utilisateur demande des scores de visibilité, des comparaisons brand vs concurrents, des évolutions temporelles, des analyses de performance GEO/AIO.
- Nécessite d'avoir d'abord appelé get_domains_and_topics pour connaître les domainId et topicId.

CE QUE ÇA RETOURNE :
- Un dataset TSV (tab-separated) avec une ligne par combinaison Date × Modèle.
- Colonnes : Date | Model | Brand_Score | Competitor1_Name | Competitor1_Score | ... | Competitor5_Name | Competitor5_Score
- Les concurrents sont triés par score décroissant (top 5 uniquement).
- Une ligne de métadonnées avec la période, les modèles disponibles et le nombre de lignes.

PARAMÈTRES :
- domainId (OBLIGATOIRE) : ID du domaine obtenu via get_domains_and_topics.
- topicId (OBLIGATOIRE) : ID du topic obtenu via get_domains_and_topics.
- startDate (OPTIONNEL) : Date de début au format YYYY-MM-DD. NE PAS REMPLIR sauf si l'utilisateur précise une date. Par défaut = 30 jours avant aujourd'hui.
- endDate (OPTIONNEL) : Date de fin au format YYYY-MM-DD. NE PAS REMPLIR sauf si l'utilisateur précise une date. Par défaut = aujourd'hui.
- models (OPTIONNEL) : Identifiant d'UN modèle IA spécifique. NE PAS REMPLIR sauf si l'utilisateur demande explicitement un modèle. Par défaut = agrégation GLOBAL (tous modèles confondus).

MODÈLES DISPONIBLES (valeurs exactes à passer dans le paramètre models) :
  - "gpt-5.1"                → OpenAI GPT-5.1
  - "gpt-5"                  → OpenAI GPT-5
  - "gpt-interface"          → ChatGPT (interface web OpenAI)
  - "gemini-3-pro-preview"   → Google Gemini 3 Pro Preview
  - "google-ai-overview"     → Google AI Overview (résultats enrichis Google Search)
  - "sonar-pro"              → Perplexity Sonar Pro

RÈGLES IMPORTANTES :
1. Si l'utilisateur ne mentionne PAS de dates → ne pas envoyer startDate ni endDate (le défaut = 30 derniers jours).
2. Si l'utilisateur ne mentionne PAS de modèle spécifique → ne pas envoyer models (le défaut = GLOBAL agrégé).
3. Si l'utilisateur veut PLUSIEURS modèles spécifiques → faire UN appel par modèle (ex: un appel avec models="gpt-5", un autre avec models="sonar-pro").
4. Le résultat GLOBAL est TOUJOURS inclus dans la réponse, même quand un modèle spécifique est demandé.""",
    inputSchema={
        "type": "object",
        "properties": {
            "domainId": {
                "type": "string",
                "description": "OBLIGATOIRE. Identifiant du domaine (marque). Obtenu via get_domains_and_topics."
            },
            "topicId": {
                "type": "string",
                "description": "OBLIGATOIRE. Identifiant du topic (sujet). Obtenu via get_domains_and_topics."
            },
            "startDate": {
                "type": "string",
                "description": "OPTIONNEL. Date de début YYYY-MM-DD. NE PAS REMPLIR si l'utilisateur ne précise pas de date. Défaut = 30 jours avant aujourd'hui."
            },
            "endDate": {
                "type": "string",
                "description": "OPTIONNEL. Date de fin YYYY-MM-DD. NE PAS REMPLIR si l'utilisateur ne précise pas de date. Défaut = aujourd'hui."
            },
            "models": {
                "type": "string",
                "description": "OPTIONNEL. UN identifiant de modèle IA. NE PAS REMPLIR si l'utilisateur ne demande pas de modèle spécifique. Valeurs possibles : gpt-5.1, gpt-5, gpt-interface, gemini-3-pro-preview, google-ai-overview, sonar-pro."
            }
        },
        "required": ["domainId", "topicId"],
        "additionalProperties": False
    }
)


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [TOOL_GET_DOMAINS_AND_TOPICS, TOOL_GET_VISIBILITY_SCORES]

@server.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    try:
        if name == "get_domains_and_topics":
            res = await get_domains_and_topics()
            return [TextContent(type="text", text=json.dumps(res, default=str))]
        elif name == "get_visibility_scores":
            res = await get_visibility_scores(**arguments)
            # Retourner metadata + TSV en texte brut (pas de JSON wrapping du TSV)
            output = f"{res['metadata']}\n\n{res['tsv_dataset']}"
            return [TextContent(type="text", text=output)]
        else:
            raise ValueError(f"Unknown tool: {name}")
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {str(e)}")]


# ========== CONFIGURATION WEB (SSE + FIX 405) ==========

sse = SseServerTransport("/messages")

async def handle_sse_connect(request: Request):
    """Gère la connexion (GET)"""
    async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
        await server.run(streams[0], streams[1], server.create_initialization_options())

async def handle_messages(request: Request):
    """Gère les messages (POST)"""
    await sse.handle_post_message(request.scope, request.receive, request._send)

# Routes explicites pour gérer le cas où le client poste sur /sse
routes = [
    Route("/sse", endpoint=handle_sse_connect, methods=["GET"]),
    Route("/sse", endpoint=handle_messages, methods=["POST"]),      # <--- C'EST ICI LE FIX IMPORTANT
    Route("/messages", endpoint=handle_messages, methods=["POST"])  # Route standard
]

middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
]

app = Starlette(debug=True, routes=routes, middleware=middleware)