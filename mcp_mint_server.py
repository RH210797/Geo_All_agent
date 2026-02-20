"""
Mint.ai Visibility MCP Server - Version 3.5.0 (Citations Explorer)

Serveur MCP (Model Context Protocol) permettant d'accéder aux données de visibilité
de marques via l'API Mint.ai. Compatible avec les clients MCP standards (Claude Desktop)
et les clients Web utilisant le transport SSE (Server-Sent Events).

Fonctionnalités principales:
- Récupération de la liste des domaines et topics disponibles
- Extraction des scores de visibilité avec historique étendu (365 jours par défaut)
- Support de multiples modèles d'IA (GPT, Gemini, Sonar, etc.)
- Format de données structuré pour l'analyse comparative
- Récupération des citations paginées avec agrégation par domaine source

Modifications version 3.5.0:
- Ajout du tool get_citations : récupération des sources citées par les LLMs dans les prompts
- Agrégation automatique : comptage du nombre de mentions par domaine source (moins de lignes)
- Paramètres de filtrage : modèle, catégorie de prompt, pagination

Modifications version 3.4.0:
- Extension de la période par défaut de 30 à 365 jours d'historique
- Augmentation de la limite de résultats de 100 à 1000 entrées
- Correction de l'erreur 405 sur l'endpoint /sse pour les clients Web

Variables d'environnement requises:
- MINT_API_KEY: Clé d'authentification pour l'API Mint.ai
- MINT_BASE_URL: URL de base de l'API (défaut: https://api.getmint.ai/api)
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

# ========== CONFIGURATION ==========
# Configuration de l'API Mint.ai via variables d'environnement
# Ces valeurs doivent être définies avant le démarrage du serveur
MINT_API_KEY = os.getenv("MINT_API_KEY", "")
MINT_BASE_URL = os.getenv("MINT_BASE_URL", "https://api.getmint.ai/api")

# Configuration du logging pour le suivi des opérations et le débogage
# Le niveau INFO permet de suivre les principales actions du serveur
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Vérification critique: la clé API est indispensable pour toutes les opérations
if not MINT_API_KEY:
    logger.warning("MINT_API_KEY environment variable is missing!")

# Création de l'instance du serveur MCP avec un nom identifiant unique
server = Server("mint-visibility-mcp")


# ========== LOGIQUE MÉTIER (API & TOOLS) ==========

async def fetch_api(path: str, params: dict = None) -> dict:
    """
    Effectue une requête GET asynchrone vers l'API Mint.ai.
    
    Cette fonction centralise tous les appels à l'API externe, gère l'authentification
    via la clé API dans les headers, et propage les erreurs HTTP.
    
    Args:
        path: Chemin de l'endpoint API (ex: "/domains" ou "/domains/{id}/topics")
        params: Dictionnaire optionnel de paramètres de requête (query parameters)
    
    Returns:
        dict: Réponse JSON désérialisée de l'API
    
    Raises:
        RuntimeError: Si MINT_API_KEY n'est pas définie
        httpx.HTTPStatusError: Si la requête échoue (4xx, 5xx)
    
    Note:
        Timeout fixé à 30 secondes pour éviter les blocages prolongés
    """
    if not MINT_API_KEY:
        raise RuntimeError("MINT_API_KEY environment variable is required")
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{MINT_BASE_URL}{path}", params=params or {}, headers={"X-API-Key": MINT_API_KEY}, timeout=30.0)
        response.raise_for_status()
        return response.json()

async def get_domains_and_topics() -> dict:
    """
    Récupère la liste complète des domaines et de leurs topics associés depuis l'API Mint.ai.
    
    Cette fonction effectue d'abord une requête pour obtenir tous les domaines disponibles,
    puis pour chaque domaine, récupère ses topics associés. Elle construit également un
    mapping pour faciliter la navigation entre domaines et topics.
    
    Returns:
        dict: Dictionnaire structuré contenant:
            - status: "success" si l'opération réussit
            - data: {
                "domains": Liste complète des domaines avec leurs métadonnées
                "topics": Liste de tous les topics avec leur domaine parent
                "mapping": Dict {"{domain} > {topic}": {"domainId": ..., "topicId": ...}}
              }
    
    Note:
        Si la récupération des topics d'un domaine échoue, l'erreur est ignorée
        silencieusement (ligne except: continue) pour ne pas bloquer le traitement
        des autres domaines. Cela pourrait masquer des problèmes d'accès ou de permission.
    
    Exemple de mapping généré:
        {"IBIS > IBIS FR": {"domainId": "694a...", "topicId": "694a..."}}
    """
    # Récupération de la liste complète des domaines disponibles
    domains = await fetch_api("/domains")
    all_topics = []
    mapping = {}
    
    # Pour chaque domaine, on récupère ses topics associés
    for domain in domains:
        d_id = domain.get("id")
        d_name = domain.get("displayName", domain.get("name", "Unknown"))
        try:
            # Appel API pour obtenir les topics du domaine courant
            topics = await fetch_api(f"/domains/{d_id}/topics")
            for topic in topics:
                t_id = topic.get("id")
                t_name = topic.get("displayName", topic.get("name", "Unknown"))
                
                # Ajout du topic à la liste globale avec référence au domaine parent
                all_topics.append({"id": t_id, "name": t_name, "domainId": d_id, "domainName": d_name})
                
                # Création d'une clé de mapping lisible pour faciliter la navigation
                mapping[f"{d_name} > {t_name}"] = {"domainId": d_id, "topicId": t_id}
        except Exception:
            # ATTENTION: Les erreurs sont ignorées silencieusement ici
            # Cela peut masquer des problèmes d'authentification ou de droits d'accès
            continue
    
    return {"status": "success", "data": {"domains": domains, "topics": all_topics, "mapping": mapping}}

async def get_visibility_scores(domainId: str, topicId: str, startDate: str = None, endDate: str = None, models: str = None) -> dict:
    """
    Récupère les scores de visibilité d'une marque et de ses concurrents sur une période donnée.
    
    Cette fonction constitue le cœur du serveur MCP. Elle interroge l'API Mint.ai pour obtenir
    les données de visibilité agrégées (score GLOBAL) ainsi que les données par modèle d'IA
    (GPT, Gemini, Sonar, etc.), puis construit un dataset structuré pour l'analyse.
    
    Args:
        domainId: Identifiant unique du domaine (marque principale)
        topicId: Identifiant unique du topic (segment géographique ou thématique)
        startDate: Date de début au format YYYY-MM-DD (optionnel, défaut: aujourd'hui - 365 jours)
        endDate: Date de fin au format YYYY-MM-DD (optionnel, défaut: aujourd'hui)
        models: Liste de modèles spécifiques à interroger (optionnel, sinon tous les modèles)
    
    Returns:
        dict: {
            "status": "success",
            "data": {
                "dataset": Liste de dictionnaires avec structure:
                    {
                        "Date": "YYYY-MM-DD",
                        "EntityName": "Nom de la marque ou du concurrent",
                        "EntityType": "Brand" ou "Competitor",
                        "Score": float (pourcentage de visibilité),
                        "Model": "GLOBAL" ou nom du modèle IA
                    },
                "metadata": {
                    "models": Liste des modèles inclus dans le dataset
                }
            }
        }
    
    Note sur les paramètres par défaut:
        - Période de 365 jours: Permet une analyse de tendances à long terme
        - Limite de 1000 résultats: Devrait couvrir l'intégralité des données pour une année
        - latestOnly=false: Récupère toutes les données historiques, pas seulement le dernier point
    
    Optimisation possible:
        Les appels API par modèle sont actuellement séquentiels. L'utilisation d'asyncio.gather
        permettrait de paralléliser ces requêtes et d'améliorer significativement les performances
        lorsque de nombreux modèles sont disponibles.
    """
    # Si aucune date n'est spécifiée, on utilise les 365 derniers jours par défaut
    # Cette période étendue (vs 30 jours en v3.3.0) permet des analyses de tendances robustes
    if not startDate or not endDate:
        endDate = date.today().strftime("%Y-%m-%d")
        startDate = (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")
    
    # Paramètres de base pour toutes les requêtes API
    # - latestOnly=false: Récupère l'historique complet, pas seulement le dernier snapshot
    # - page=1: Pagination (non utilisée actuellement, mais pourrait être implémentée)
    # - limit=1000: Nombre maximum de points de données (augmenté de 100 à 1000 en v3.4.0)
    base_params = {"startDate": startDate, "endDate": endDate, "latestOnly": "false", "page": "1", "limit": "1000"}
    
    # Récupération des données agrégées GLOBAL (tous modèles confondus)
    # Cette requête retourne également la liste des modèles disponibles pour ce domaine/topic
    global_data = await fetch_api(f"/domains/{domainId}/topics/{topicId}/visibility/aggregated", base_params)
    available_models = global_data.get("availableModels", [])
    
    # Récupération des données par modèle individuel (GPT-5, Gemini, Sonar, etc.)
    # ATTENTION: Cette boucle effectue des appels séquentiels qui pourraient être parallélisés
    # avec asyncio.gather pour améliorer les performances
    by_model_data = {}
    for m in available_models:
        try:
            # Pour chaque modèle, on refait un appel avec le filtre "models" spécifique
            by_model_data[m] = await fetch_api(f"/domains/{domainId}/topics/{topicId}/visibility/aggregated", {**base_params, "models": m})
        except:
            # Les erreurs sont ignorées silencieusement pour éviter qu'un modèle défaillant
            # ne bloque l'intégralité de la récupération. Cependant, cela masque les problèmes.
            pass

    # Construction du dataset unifié au format structuré
    # Chaque ligne représente un score (marque ou concurrent) à une date donnée pour un modèle
    dataset = []
    
    def add_rows(data, model_name):
        """
        Fonction interne pour transformer les données chartData de l'API en lignes de dataset.
        
        Structure de chartData de l'API:
        [
            {
                "date": "2026-01-13",
                "brand": 50.76,
                "competitors": {"Booking": 30, "B&B Hotels": 26, ...}
            },
            ...
        ]
        
        Transformation en dataset:
        - Une ligne pour la marque principale
        - Une ligne pour chaque concurrent
        - Toutes liées à la même date et au même modèle
        """
        for entry in data.get("chartData", []):
            d = entry.get("date")
            # Ajout du score de la marque principale
            dataset.append({"Date": d, "EntityName": "Brand", "EntityType": "Brand", "Score": entry.get("brand"), "Model": model_name})
            # Ajout des scores de tous les concurrents pour cette date
            for c_name, c_score in entry.get("competitors", {}).items():
                dataset.append({"Date": d, "EntityName": c_name, "EntityType": "Competitor", "Score": c_score, "Model": model_name})

    # Ajout des données GLOBAL (agrégées tous modèles)
    add_rows(global_data, "GLOBAL")
    
    # Ajout des données par modèle individuel
    for m, data in by_model_data.items():
        add_rows(data, m)

    return {"status": "success", "data": {"dataset": dataset, "metadata": {"models": ["GLOBAL"] + available_models}}}


async def get_citations(
    domainId: str,
    topicId: str,
    startDate: str = None,
    endDate: str = None,
    models: str = None,
) -> dict:
    """
    Récupère les top domaines et top URLs cités pour un topic donné,
    en bouclant sur chaque modèle disponible (même logique que get_visibility_scores).

    Utilise l'endpoint visibility/aggregated avec includeDetailedResults=true
    qui retourne directement topDomains, topCitedUrls, topDomainsOverTime, etc.
    → Pas de pagination, 1 seul call par modèle.

    Args:
        domainId:   ID du domaine (REQUIS)
        topicId:    ID du topic (REQUIS)
        startDate:  Date début YYYY-MM-DD (défaut: aujourd'hui - 90j)
        endDate:    Date fin   YYYY-MM-DD (défaut: aujourd'hui)
        models:     Modèles à inclure, séparés par virgule (optionnel, défaut: tous)

    Returns:
        dict avec :
          - top_domains  : [{Model, Domain, CitationCount, Rank}, ...]
          - top_urls     : [{Model, Url, Domain, CitationCount, Rank}, ...]
          - domains_over_time : [{Model, Date, Domain, Count}, ...]
          - urls_over_time    : [{Model, Date, Url, Count}, ...]
          - global_metrics    : [{Model, TotalPrompts, TotalAnswers, TotalCitations, ReportCount}, ...]
    """
    if not startDate or not endDate:
        endDate   = date.today().strftime("%Y-%m-%d")
        startDate = (date.today() - timedelta(days=90)).strftime("%Y-%m-%d")

    base_params = {
        "startDate":              startDate,
        "endDate":                endDate,
        "includeDetailedResults": "true",
        "latestOnly":             "false",
        "page":                   "1",
        "limit":                  "1000",  # max pour récupérer tous les top domaines/URLs sans troncature
    }

    endpoint = f"/domains/{domainId}/topics/{topicId}/visibility/aggregated"

    # ── Récupération GLOBAL + liste des modèles disponibles ──────────────────
    global_data      = await fetch_api(endpoint, base_params)
    available_models = global_data.get("availableModels", [])

    # Filtre optionnel sur les modèles
    if models:
        requested = [m.strip() for m in models.split(",")]
        available_models = [m for m in available_models if m in requested]

    # ── Récupération par modèle en parallèle ─────────────────────────────────
    async def fetch_model(m):
        try:
            return m, await fetch_api(endpoint, {**base_params, "models": m})
        except Exception:
            return m, None

    tasks = [fetch_model(m) for m in available_models]
    model_results = await asyncio.gather(*tasks)
    by_model = {m: d for m, d in model_results if d is not None}

    # ── Extraction helper ─────────────────────────────────────────────────────
    def extract(data, model_name):
        top_domains, top_urls, domains_ot, urls_ot, metrics = [], [], [], [], []

        # topDomains
        for i, item in enumerate(data.get("topDomains", []), 1):
            top_domains.append({
                "Model":         model_name,
                "Domain":        item.get("domain", item.get("linkDomain", "")),
                "CitationCount": item.get("count",  item.get("citationCount", 0)),
                "Rank":          i,
            })

        # topCitedUrls
        for i, item in enumerate(data.get("topCitedUrls", []), 1):
            top_urls.append({
                "Model":         model_name,
                "Url":           item.get("url",    item.get("link", "")),
                "Domain":        item.get("domain", item.get("linkDomain", "")),
                "CitationCount": item.get("count",  item.get("citationCount", 0)),
                "Rank":          i,
            })

        # topDomainsOverTime
        for entry in data.get("topDomainsOverTime", []):
            for domain, count in entry.get("domains", {}).items():
                domains_ot.append({
                    "Model":  model_name,
                    "Date":   entry.get("date", ""),
                    "Domain": domain,
                    "Count":  count,
                })

        # topUrlsOverTime
        for entry in data.get("topUrlsOverTime", []):
            for url, count in entry.get("urls", {}).items():
                urls_ot.append({
                    "Model": model_name,
                    "Date":  entry.get("date", ""),
                    "Url":   url,
                    "Count": count,
                })

        # global metrics
        metrics.append({
            "Model":         model_name,
            "TotalPrompts":  data.get("totalPromptsTested", 0),
            "TotalAnswers":  data.get("totalAnswers",        0),
            "TotalCitations":data.get("totalCitations",     0),
            "ReportCount":   data.get("reportCount",        0),
        })

        return top_domains, top_urls, domains_ot, urls_ot, metrics

    # ── Assemblage du dataset final ───────────────────────────────────────────
    all_top_domains, all_top_urls, all_domains_ot, all_urls_ot, all_metrics = [], [], [], [], []

    # GLOBAL d'abord
    td, tu, dot, uot, met = extract(global_data, "GLOBAL")
    all_top_domains  += td;  all_top_urls    += tu
    all_domains_ot   += dot; all_urls_ot     += uot
    all_metrics      += met

    # Puis chaque modèle
    for m, data in by_model.items():
        td, tu, dot, uot, met = extract(data, m)
        all_top_domains  += td;  all_top_urls    += tu
        all_domains_ot   += dot; all_urls_ot     += uot
        all_metrics      += met

    return {
        "status": "success",
        "data": {
            "top_domains":      all_top_domains,
            "top_urls":         all_top_urls,
            "domains_over_time":all_domains_ot,
            "urls_over_time":   all_urls_ot,
            "global_metrics":   all_metrics,
            "metadata": {
                "models": ["GLOBAL"] + list(by_model.keys()),
                "startDate": startDate,
                "endDate":   endDate,
            },
        },
    }

# ========== ENREGISTREMENT DES OUTILS MCP ==========

@server.list_tools()
async def list_tools() -> list[Tool]:
    """
    Déclare la liste des outils (tools) disponibles dans ce serveur MCP.
    
    Cette fonction est appelée automatiquement par le client MCP lors de la connexion
    pour découvrir les capacités du serveur. Chaque outil déclaré ici devient accessible
    via l'interface call_tool().
    
    Returns:
        list[Tool]: Liste des outils MCP avec leurs schémas de validation
    
    Outils disponibles:
        1. get_domains_and_topics: Exploration de la hiérarchie domaines/topics
        2. get_visibility_scores: Récupération des données de visibilité avec historique
    """
    return [
        Tool(
            name="get_domains_and_topics",
            description="🌍 Liste tous les domaines et topics disponibles. Utilise cet outil en premier.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="get_visibility_scores",
            description="📈 Récupère les scores de visibilité en dataset tabulaire. Paramètres optionnels: startDate/endDate (YYYY-MM-DD), models (GLOBAL,gpt-5.1,sonar-pro,google-ai-overview,gpt-interface,gemini-3-pro-preview,gpt-5). Si omis → retour complet.",
            inputSchema={
                "type": "object",
                "properties": {
                    "domainId": {"type": "string", "description": "ID du domaine (REQUIS)"},
                    "topicId": {"type": "string", "description": "ID du topic (REQUIS)"},
                    "startDate": {"type": "string", "description": "Date début YYYY-MM-DD (optionnel)"},
                    "endDate": {"type": "string", "description": "Date fin YYYY-MM-DD (optionnel)"},
                    "models": {"type": "string", "description": "Modèles à filtrer (optionnel, séparés par virgule)"}
                },
                "required": ["domainId", "topicId"]
            }
        ),
        Tool(
            name="get_citations",
            description="🔗 Récupère les top domaines et top URLs cités par les LLMs, par modèle. Boucle sur tous les modèles disponibles (GLOBAL + GPT-5, Gemini, Sonar...). Retourne: top_domains, top_urls, domains_over_time, urls_over_time, global_metrics. Paramètres optionnels: startDate/endDate (YYYY-MM-DD, défaut 90j), models (séparés par virgule).",
            inputSchema={
                "type": "object",
                "properties": {
                    "domainId":  {"type": "string", "description": "ID du domaine (REQUIS)"},
                    "topicId":   {"type": "string", "description": "ID du topic (REQUIS)"},
                    "startDate": {"type": "string", "description": "Date début YYYY-MM-DD (optionnel, défaut: aujourd'hui - 90 jours)"},
                    "endDate":   {"type": "string", "description": "Date fin YYYY-MM-DD (optionnel, défaut: aujourd'hui)"},
                    "models":    {"type": "string", "description": "Modèles à inclure, séparés par virgule (optionnel, défaut: tous)"},
                },
                "required": ["domainId", "topicId"]
            }
        )
    ]

@server.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    """
    Point d'entrée pour l'exécution des outils MCP.
    
    Cette fonction est appelée par le client MCP lorsqu'il souhaite exécuter un outil.
    Elle route la demande vers la fonction appropriée et gère les erreurs de manière centralisée.
    
    Args:
        name: Nom de l'outil à exécuter (doit correspondre à un outil déclaré dans list_tools)
        arguments: Dictionnaire d'arguments passés à l'outil (validés selon le inputSchema)
    
    Returns:
        list[TextContent]: Réponse encapsulée au format MCP (JSON sérialisé en texte)
    
    Gestion des erreurs:
        Toutes les exceptions sont capturées et retournées sous forme de message d'erreur textuel.
        ATTENTION: Cette approche masque les détails des erreurs. Une gestion plus granulaire
        permettrait de distinguer les erreurs d'authentification, de validation, de réseau, etc.
    """
    try:
        # Routage vers la fonction appropriée selon le nom de l'outil
        if name == "get_domains_and_topics":
            res = await get_domains_and_topics()
        elif name == "get_visibility_scores":
            # Expansion des arguments du dictionnaire comme paramètres nommés
            res = await get_visibility_scores(**arguments)
        elif name == "get_citations":
            res = await get_citations(**arguments)
        else:
            raise ValueError(f"Unknown tool: {name}")
        
        # Sérialisation de la réponse en JSON, avec gestion des types non-standard (dates, etc.)
        return [TextContent(type="text", text=json.dumps(res, default=str))]
    except Exception as e:
        # AMÉLIORATION POSSIBLE: Distinguer les types d'erreurs pour des messages plus précis
        # - AuthenticationError → "Clé API invalide ou expirée"
        # - ValidationError → "Paramètres invalides: {détails}"
        # - NetworkError → "Impossible de joindre l'API Mint.ai"
        return [TextContent(type="text", text=f"Error: {str(e)}")]


# ========== CONFIGURATION WEB (TRANSPORT SSE & ROUTING) ==========

# Création du transport SSE (Server-Sent Events) pour la communication MCP
# L'endpoint /messages est la route standard pour les clients MCP stricts (Claude Desktop)
sse = SseServerTransport("/messages")

async def handle_sse_connect(request: Request):
    """
    Gère la connexion initiale SSE (requête GET).
    
    Cette fonction est appelée lorsqu'un client MCP établit une connexion SSE.
    Elle crée les streams de communication bidirectionnels et lance la boucle
    principale du serveur MCP pour traiter les messages entrants.
    
    Args:
        request: Requête HTTP Starlette contenant scope, receive et send
    
    Note:
        Cette fonction reste active pendant toute la durée de la session MCP.
        Elle ne se termine que lorsque le client ferme la connexion.
    """
    async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
        # Démarrage de la boucle principale du serveur MCP avec les streams de communication
        await server.run(streams[0], streams[1], server.create_initialization_options())

async def handle_messages(request: Request):
    """
    Gère les messages entrants (requête POST).
    
    Cette fonction traite les messages JSON-RPC envoyés par le client MCP via POST.
    Elle est appelée pour chaque invocation d'outil ou requête du client après
    l'établissement de la connexion SSE initiale.
    
    Args:
        request: Requête HTTP POST contenant le message JSON-RPC
    """
    await sse.handle_post_message(request.scope, request.receive, request._send)

# ========== DÉFINITION DES ROUTES HTTP ==========
# 
# Configuration critique pour la compatibilité multi-clients:
# - Claude Desktop et clients MCP stricts utilisent /messages (GET + POST)
# - Certains clients Web et interfaces custom utilisent /sse (GET + POST)
# 
# Le problème résolu ici (version 3.3.0):
# Avant, seul GET était configuré sur /sse, causant des erreurs 405 (Method Not Allowed)
# lorsque des clients Web tentaient de POST des messages sur cet endpoint.
# 
# Solution: Définir explicitement GET et POST sur les deux endpoints (/sse et /messages)

routes = [
    # Endpoint /sse pour les clients Web et interfaces custom
    Route("/sse", endpoint=handle_sse_connect, methods=["GET"]),   # Connexion SSE initiale
    Route("/sse", endpoint=handle_messages, methods=["POST"]),      # Messages JSON-RPC (FIX v3.3.0)
    
    # Endpoint /messages pour les clients MCP stricts (standard du protocole)
    Route("/messages", endpoint=handle_messages, methods=["POST"])  # Messages JSON-RPC
]

# Configuration CORS (Cross-Origin Resource Sharing)
# Permet l'accès au serveur depuis n'importe quelle origine (développement/production)
# SÉCURITÉ: En production, il serait recommandé de restreindre allow_origins
# à une liste explicite de domaines autorisés plutôt que d'utiliser "*"
middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=["*"],        # ATTENTION: Accepte toutes les origines (permissif)
        allow_methods=["*"],        # Autorise tous les verbes HTTP
        allow_headers=["*"],        # Autorise tous les headers
    )
]

# Création de l'application Starlette avec la configuration complète
# - debug=True: Active le mode débogage (à désactiver en production)
# - routes: Configuration des endpoints HTTP
# - middleware: Stack de middlewares (CORS uniquement pour l'instant)
app = Starlette(debug=True, routes=routes, middleware=middleware)