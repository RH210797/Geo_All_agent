"""
Mint.ai Visibility MCP Server - Version 3.2.0 (Stable ASGI Mount)

Serveur MCP pour analyser la visibilité de marque dans les LLMs via l'API Mint.ai
Compatible Render/Railway avec correction "Mount" pour la stabilité.
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
from mcp.types import Tool, TextContent
from mcp.server.sse import SseServerTransport

# Imports Starlette (Serveur Web)
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

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
    """Récupère tous les domaines et topics disponibles"""
    domains = await fetch_api("/domains")
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
                    "id": topic_id, "name": topic_name,
                    "domainId": domain_id, "domainName": domain_name
                })
                mapping[f"{domain_name} > {topic_name}"] = {
                    "domainId": domain_id, "topicId": topic_id
                }
        except Exception as e:
            logger.warning(f"Error fetching topics for domain {domain_id}: {e}")
            continue
    
    return {
        "status": "success",
        "data": {"domains": domains, "topics": all_topics, "mapping": mapping}
    }


# ========== TOOL 2: get_visibility_scores ==========

async def get_visibility_scores(domain_id: str, topic_id: str, start_date: str = None, end_date: str = None, models: str = None) -> dict:
    """Récupère les scores de visibilité avec analyse par modèle"""
    if not start_date or not end_date:
        end = date.today()
        start = end - timedelta(days=30)
        start_date = start.strftime("%Y-%m-%d")
        end_date = end.strftime("%Y-%m-%d")
    
    base_params = {"startDate": start_date, "endDate": end_date, "latestOnly": "false", "page": "1", "limit": "100"}
    
    logger.info(f"Fetching global data for domain={domain_id}, topic={topic_id}")
    global_data = await fetch_api(f"/domains/{domain_id}/topics/{topic_id}/visibility/aggregated", base_params)
    
    available_models = global_data.get("availableModels", [])
    by_model_data = {}
    
    for model_name in available_models:
        try:
            params_model = {**base_params, "models": model_name}
            model_data = await fetch_api(f"/domains/{domain_id}/topics/{topic_id}/visibility/aggregated", params_model)
            by_model_data[model_name] = model_data
        except Exception:
            continue
            
    return {"status": "success", "data": build_dataset(global_data, by_model_data, available_models)}


def build_dataset(global_data: dict, by_model_data: dict, available_models: list) -> dict:
    """Construit le dataset structuré"""
    all_rows = []
    brand_name = "Your Brand"
    
    # Helper pour ajouter des lignes
    def process_chart_data(data, model_label):
        chart_data = data.get("chartData", [])
        for i, entry in enumerate(chart_data):
            date_str = entry.get("date")
            # Brand
            brand_score = entry.get("brand", 0)
            if i > 0:
                prev = chart_data[i-1].get("brand", 0)
                var = round(brand_score - prev, 2)
                var_pct = round((var / prev) * 100, 2) if prev > 0 else 0
            else:
                var, var_pct = None, None
            
            all_rows.append({
                "Date": date_str, "EntityName": brand_name, "EntityType": "Brand",
                "Score": brand_score, "Model": model_label,
                "Variation_Points": var, "Variation_Percent": var_pct
            })
            
            # Competitors
            for comp_name, comp_score in entry.get("competitors", {}).items():
                if comp_score > 0:
                    if i > 0:
                        prev_comp = chart_data[i-1].get("competitors", {}).get(comp_name, 0)
                        c_var = round(comp_score - prev_comp, 2)
                        c_var_pct = round((c_var / prev_comp) * 100, 2) if prev_comp > 0 else 0
                    else:
                        c_var, c_var_pct = None, None
                    
                    all_rows.append({
                        "Date": date_str, "EntityName": comp_name, "EntityType": "Competitor",
                        "Score": comp_score, "Model": model_label,
                        "Variation_Points": c_var, "Variation_Percent": c_var_pct
                    })

    # Process Global & Models
    process_chart_data(global_data, "GLOBAL")
    for m_name, m_data in by_model_data.items():
        process_chart_data(m_data, m_name)

    return {
        "dataset": all_rows,
        "metadata": {
            "totalRows": len(all_rows),
            "models": ["GLOBAL"] + available_models
        }
    }


# ========== MCP HANDLERS ==========

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_domains_and_topics",
            description="Liste TOUS les domaines et topics disponibles dans Mint.ai.",
            inputSchema={"type": "object", "properties": {}, "required": []}
        ),
        Tool(
            name="get_visibility_scores",
            description="Analyse COMPLÈTE de visibilité avec split par modèle LLM. Format: Date|Entity|Score|Model.",
            inputSchema={
                "type": "object",
                "properties": {
                    "domainId": {"type": "string"},
                    "topicId": {"type": "string"},
                    "startDate": {"type": "string"},
                    "endDate": {"type": "string"},
                    "models": {"type": "string"}
                },
                "required": ["domainId", "topicId"]
            }
        )
    ]

@server.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    try:
        if name == "get_domains_and_topics":
            result = await get_domains_and_topics()
        elif name == "get_visibility_scores":
            result = await get_visibility_scores(**arguments)
        else:
            raise ValueError(f"Unknown tool: {name}")
        
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}, indent=2))]


# ========== SERVER-SENT EVENTS (SSE) & CORS ==========

sse = SseServerTransport("/messages")

async def handle_sse(scope, receive, send):
    """
    Handler RAW ASGI pour la connexion SSE.
    Ne retourne rien, gère directement le flux.
    """
    async with sse.connect_sse(scope, receive, send) as streams:
        await server.run(
            streams[0],
            streams[1],
            server.create_initialization_options()
        )

async def handle_messages(scope, receive, send):
    """Handler RAW ASGI pour les messages POST"""
    await sse.handle_post_message(scope, receive, send)

# Middleware CORS (Obligatoire pour que ChatGPT/Claude puisse se connecter)
middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
]

# Note importante : On utilise Mount() au lieu de Route()
# Cela permet de passer des applications ASGI brutes sans que Starlette
# n'essaie de gérer la réponse (ce qui causait l'erreur précédente).
app = Starlette(
    debug=True,
    routes=[
        Mount("/sse", app=handle_sse),
        Mount("/messages", app=handle_messages)
    ],
    middleware=middleware
)