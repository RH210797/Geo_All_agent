"""
Mint.ai Visibility MCP Server - Version 3.0.0 (Web/SSE Edition)

Serveur MCP pour analyser la visibilité de marque dans les LLMs via l'API Mint.ai
Compatible avec Render/Railway via SSE (Server-Sent Events).

Tools disponibles:
1. get_domains_and_topics - Liste tous les domaines et topics
2. get_visibility_scores - Dataset complet (Date | EntityName | EntityType | Score | Model | Variation)
"""

import asyncio
import json
import logging
import os
import sys
from datetime import date, timedelta
from typing import Any, Optional

import httpx
from mcp.server import Server
# Note: On n'utilise plus stdio_server pour le web
from mcp.types import Tool, TextContent

# Nouveaux imports pour le mode Web (SSE)
from starlette.applications import Starlette
from starlette.routing import Route
from mcp.server.sse import SseServerTransport

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


# ========== API CLIENT ==========

async def fetch_api(path: str, params: dict = None) -> dict:
    """Effectue une requête GET vers l'API Mint.ai"""
    if not MINT_API_KEY:
        raise RuntimeError("MINT_API_KEY environment variable is required")
        
    url = f"{MINT_BASE_URL}{path}"
    headers = {"X-API-Key": MINT_API_KEY}
    
    async with httpx.AsyncClient() as client:
        response = await client.get(url, params=params or {}, headers=headers, timeout=30.0)
        response.raise_for_status()
        return response.json()


# ========== TOOL 1: get_domains_and_topics ==========

async def get_domains_and_topics() -> dict:
    """
    Récupère tous les domaines et topics disponibles
    """
    # Récupérer les domaines
    domains = await fetch_api("/domains")
    
    # Pour chaque domaine, récupérer ses topics
    all_topics = []
    mapping = {}
    
    for domain in domains:
        domain_id = domain.get("id")
        domain_name = domain.get("displayName", domain.get("name", "Unknown"))
        
        try:
            topics = await fetch_api(f"/domains/{domain_id}/topics")
            
            for topic in topics:
                topic_id = topic.get("id")
                topic_name = topic.get("displayName", topic.get("name", "Unknown"))
                
                all_topics.append({
                    "id": topic_id,
                    "name": topic_name,
                    "domainId": domain_id,
                    "domainName": domain_name
                })
                
                mapping[f"{domain_name} > {topic_name}"] = {
                    "domainId": domain_id,
                    "topicId": topic_id,
                    "domainName": domain_name,
                    "topicName": topic_name
                }
        except Exception as e:
            logger.warning(f"Error fetching topics for domain {domain_id}: {e}")
            continue
    
    return {
        "status": "success",
        "data": {
            "domains": domains,
            "topics": all_topics,
            "mapping": mapping,
            "summary": {
                "totalDomains": len(domains),
                "totalTopics": len(all_topics)
            }
        }
    }


# ========== TOOL 2: get_visibility_scores ==========

async def get_visibility_scores(
    domain_id: str,
    topic_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    models: Optional[str] = None
) -> dict:
    """
    Récupère les scores de visibilité avec analyse par modèle
    """
    
    # Calcul des dates par défaut
    if not start_date or not end_date:
        end = date.today()
        start = end - timedelta(days=30)
        start_date = start.strftime("%Y-%m-%d")
        end_date = end.strftime("%Y-%m-%d")
    
    # Paramètres communs
    base_params = {
        "startDate": start_date,
        "endDate": end_date,
        "latestOnly": "false",
        "page": "1",
        "limit": "100"
    }
    
    # 1. Appel global (tous modèles confondus)
    logger.info(f"Fetching global data for domain={domain_id}, topic={topic_id}")
    global_data = await fetch_api(
        f"/domains/{domain_id}/topics/{topic_id}/visibility/aggregated",
        base_params
    )
    
    # 2. Récupérer les modèles disponibles
    available_models = global_data.get("availableModels", [])
    logger.info(f"Available models: {available_models}")
    
    # 3. Appels par modèle
    by_model_data = {}
    for model_name in available_models:
        params_model = {**base_params, "models": model_name}
        
        try:
            logger.info(f"Fetching data for model: {model_name}")
            model_data = await fetch_api(
                f"/domains/{domain_id}/topics/{topic_id}/visibility/aggregated",
                params_model
            )
            by_model_data[model_name] = model_data
        except Exception as e:
            logger.warning(f"Error fetching data for model {model_name}: {e}")
            continue
    
    # 4. Construire le dataset
    dataset = build_dataset(global_data, by_model_data, available_models)
    
    return {
        "status": "success",
        "data": dataset
    }


def build_dataset(global_data: dict, by_model_data: dict, available_models: list) -> dict:
    """
    Construit le dataset structuré
    """
    all_rows = []
    brand_name = "Your Brand"  # Peut être personnalisé
    
    # ===== GLOBAL (tous modèles confondus) =====
    chart_data_global = global_data.get("chartData", [])
    
    for i, entry in enumerate(chart_data_global):
        date_str = entry.get("date")
        brand_score = entry.get("brand", 0)
        competitors = entry.get("competitors", {})
        
        # Brand - Variation vs période précédente
        if i > 0:
            prev_score = chart_data_global[i-1].get("brand", 0)
            variation = round(brand_score - prev_score, 2)
            variation_pct = round((variation / prev_score) * 100, 2) if prev_score > 0 else 0
        else:
            variation = None
            variation_pct = None
        
        all_rows.append({
            "Date": date_str,
            "EntityName": brand_name,
            "EntityType": "Brand",
            "Score": brand_score,
            "Model": "GLOBAL",
            "Variation_Points": variation,
            "Variation_Percent": variation_pct
        })
        
        # Competitors
        for comp_name, comp_score in competitors.items():
            if comp_score > 0:
                if i > 0:
                    prev_competitors = chart_data_global[i-1].get("competitors", {})
                    prev_comp_score = prev_competitors.get(comp_name, 0)
                    comp_variation = round(comp_score - prev_comp_score, 2)
                    comp_variation_pct = round((comp_variation / prev_comp_score) * 100, 2) if prev_comp_score > 0 else 0
                else:
                    comp_variation = None
                    comp_variation_pct = None
                
                all_rows.append({
                    "Date": date_str,
                    "EntityName": comp_name,
                    "EntityType": "Competitor",
                    "Score": comp_score,
                    "Model": "GLOBAL",
                    "Variation_Points": comp_variation,
                    "Variation_Percent": comp_variation_pct
                })
    
    # ===== PAR MODÈLE =====
    for model_name, model_data in by_model_data.items():
        chart_data_model = model_data.get("chartData", [])
        
        for i, entry in enumerate(chart_data_model):
            date_str = entry.get("date")
            brand_score = entry.get("brand", 0)
            competitors = entry.get("competitors", {})
            
            # Brand
            if i > 0:
                prev_score = chart_data_model[i-1].get("brand", 0)
                variation = round(brand_score - prev_score, 2)
                variation_pct = round((variation / prev_score) * 100, 2) if prev_score > 0 else 0
            else:
                variation = None
                variation_pct = None
            
            all_rows.append({
                "Date": date_str,
                "EntityName": brand_name,
                "EntityType": "Brand",
                "Score": brand_score,
                "Model": model_name,
                "Variation_Points": variation,
                "Variation_Percent": variation_pct
            })
            
            # Competitors
            for comp_name, comp_score in competitors.items():
                if comp_score > 0:
                    if i > 0:
                        prev_competitors = chart_data_model[i-1].get("competitors", {})
                        prev_comp_score = prev_competitors.get(comp_name, 0)
                        comp_variation = round(comp_score - prev_comp_score, 2)
                        comp_variation_pct = round((comp_variation / prev_comp_score) * 100, 2) if prev_comp_score > 0 else 0
                    else:
                        comp_variation = None
                        comp_variation_pct = None
                    
                    all_rows.append({
                        "Date": date_str,
                        "EntityName": comp_name,
                        "EntityType": "Competitor",
                        "Score": comp_score,
                        "Model": model_name,
                        "Variation_Points": comp_variation,
                        "Variation_Percent": comp_variation_pct
                    })
    
    # Statistiques
    brand_rows = len([r for r in all_rows if r["EntityType"] == "Brand"])
    competitor_rows = len([r for r in all_rows if r["EntityType"] == "Competitor"])
    unique_competitors = len(set(r["EntityName"] for r in all_rows if r["EntityType"] == "Competitor"))
    
    return {
        "dataset": all_rows,
        "metadata": {
            "totalRows": len(all_rows),
            "brandRows": brand_rows,
            "competitorRows": competitor_rows,
            "uniqueCompetitors": unique_competitors,
            "modelsAnalyzed": len(available_models) + 1,  # +1 pour GLOBAL
            "models": ["GLOBAL"] + available_models,
            "dateRange": {
                "start": all_rows[0]["Date"] if all_rows else None,
                "end": all_rows[-1]["Date"] if all_rows else None
            }
        },
        "columns": ["Date", "EntityName", "EntityType", "Score", "Model", "Variation_Points", "Variation_Percent"]
    }


# ========== MCP HANDLERS ==========

@server.list_tools()
async def list_tools() -> list[Tool]:
    """Liste les tools disponibles"""
    return [
        Tool(
            name="get_domains_and_topics",
            description=(
                "Liste TOUS les domaines et topics disponibles dans Mint.ai. "
                "Utilise cet outil EN PREMIER quand l'utilisateur mentionne un nom "
                "de domaine/topic sans fournir les IDs."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        Tool(
            name="get_visibility_scores",
            description=(
                "Analyse COMPLÈTE de visibilité avec split par modèle LLM. "
                "Retourne un dataset structuré avec: Date | EntityName (Brand ou Concurrent) | "
                "EntityType | Score | Model (GLOBAL ou nom du modèle) | Variation_Points | Variation_Percent. "
                "Inclut évolutions temporelles pour la marque ET les concurrents sur chaque modèle LLM."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "domainId": {
                        "type": "string",
                        "description": "ID du domaine (utilise get_domains_and_topics pour le trouver)"
                    },
                    "topicId": {
                        "type": "string",
                        "description": "ID du topic (utilise get_domains_and_topics pour le trouver)"
                    },
                    "startDate": {
                        "type": "string",
                        "description": "Date de début au format YYYY-MM-DD (optionnel, défaut: 30 jours avant aujourd'hui)"
                    },
                    "endDate": {
                        "type": "string",
                        "description": "Date de fin au format YYYY-MM-DD (optionnel, défaut: aujourd'hui)"
                    },
                    "models": {
                        "type": "string",
                        "description": "Filtre optionnel sur les modèles (séparés par virgules)"
                    }
                },
                "required": ["domainId", "topicId"]
            }
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    """Exécute un tool"""
    
    try:
        if name == "get_domains_and_topics":
            result = await get_domains_and_topics()
            return [TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, indent=2)
            )]
        
        elif name == "get_visibility_scores":
            domain_id = arguments.get("domainId")
            topic_id = arguments.get("topicId")
            start_date = arguments.get("startDate")
            end_date = arguments.get("endDate")
            models = arguments.get("models")
            
            if not domain_id or not topic_id:
                raise ValueError("domainId and topicId are required")
            
            result = await get_visibility_scores(
                domain_id=domain_id,
                topic_id=topic_id,
                start_date=start_date,
                end_date=end_date,
                models=models
            )
            
            return [TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, indent=2)
            )]
        
        else:
            raise ValueError(f"Unknown tool: {name}")
    
    except Exception as e:
        logger.error(f"Error executing tool {name}: {e}", exc_info=True)
        return [TextContent(
            type="text",
            text=json.dumps({
                "status": "error",
                "error": str(e),
                "tool": name
            }, ensure_ascii=False, indent=2)
        )]


# ========== SERVER-SENT EVENTS (SSE) CONFIGURATION ==========
# Cette partie remplace le bloc 'stdio_server' pour fonctionner sur le Web

sse = SseServerTransport("/messages")

async def handle_sse(request):
    """Gère la connexion SSE initiale"""
    async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
        await server.run(
            streams[0],
            streams[1],
            server.create_initialization_options()
        )

async def handle_messages(request):
    """Gère les messages entrants (POST)"""
    await sse.handle_post_message(request.scope, request.receive, request._send)

# Création de l'application Starlette (c'est ce que Render va lancer)
app = Starlette(
    debug=True,
    routes=[
        Route("/sse", endpoint=handle_sse),
        Route("/messages", endpoint=handle_messages, methods=["POST"])
    ]
)