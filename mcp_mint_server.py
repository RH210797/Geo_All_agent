"""
Mint.ai Visibility MCP Server - Version 3.3.0 (Universal Fix)

Serveur MCP compatible avec les clients stricts (Claude) ET les clients Web
qui envoient les messages sur /sse (Correction erreur 405).
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
    if not startDate or not endDate:
        endDate = date.today().strftime("%Y-%m-%d")
        startDate = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")
    
    base_params = {"startDate": startDate, "endDate": endDate, "latestOnly": "false", "page": "1", "limit": "100"}
    
    # Récupération Global
    global_data = await fetch_api(f"/domains/{domainId}/topics/{topicId}/visibility/aggregated", base_params)
    available_models = global_data.get("availableModels", [])
    
    # Récupération par modèle (simplifiée pour la vitesse)
    by_model_data = {}
    for m in available_models:
        try:
            by_model_data[m] = await fetch_api(f"/domains/{domainId}/topics/{topicId}/visibility/aggregated", {**base_params, "models": m})
        except: pass

    # Construction dataset
    dataset = []
    def add_rows(data, model_name):
        for entry in data.get("chartData", []):
            d = entry.get("date")
            dataset.append({"Date": d, "EntityName": "Brand", "EntityType": "Brand", "Score": entry.get("brand"), "Model": model_name})
            for c_name, c_score in entry.get("competitors", {}).items():
                dataset.append({"Date": d, "EntityName": c_name, "EntityType": "Competitor", "Score": c_score, "Model": model_name})

    add_rows(global_data, "GLOBAL")
    for m, data in by_model_data.items(): add_rows(data, m)

    return {"status": "success", "data": {"dataset": dataset, "metadata": {"models": ["GLOBAL"] + available_models}}}

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="get_domains_and_topics", description="Liste domaines et topics", inputSchema={"type": "object", "properties": {}}),
        Tool(name="get_visibility_scores", description="Scores visibilité complets", inputSchema={"type": "object", "properties": {"domainId": {"type": "string"}, "topicId": {"type": "string"}, "startDate": {"type": "string"}, "endDate": {"type": "string"}, "models": {"type": "string"}}, "required": ["domainId", "topicId"]})
    ]

@server.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    try:
        if name == "get_domains_and_topics": res = await get_domains_and_topics()
        elif name == "get_visibility_scores": res = await get_visibility_scores(**arguments)
        else: raise ValueError(f"Unknown tool: {name}")
        return [TextContent(type="text", text=json.dumps(res, default=str))]
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