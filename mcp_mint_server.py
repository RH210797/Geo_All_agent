"""
Mint.ai Visibility MCP Server - Version 3.5.2 (LLM Guidance Advanced)

Ce serveur MCP retourne des DATASETS TABULAIRES lisibles.
Les commentaires détaillés ci-dessous guident le LLM sur TOUS les paramètres,
en particulier les paramètres OPTIONNELS (dates, models).

════════════════════════════════════════════════════════════════════════════════
GUIDE COMPLET POUR LE LLM:
════════════════════════════════════════════════════════════════════════════════

1️⃣ CHERCHER UN DOMAINE/TOPIC?
   → Utilise d'abord: get_domains_and_topics()
   → Cela retourne la liste des IDs disponibles + mapping
   → Exemple: "IBIS France" → domainId: "694a6c9c..." + topicId: "694a6d61..."

2️⃣ ANALYSER LA VISIBILITÉ?
   → Utilise: get_visibility_scores(domainId, topicId, output_format="tabular")
   → Cela retourne une BELLE TABLE avec:
      - Lignes: Date + Model
      - Colonnes: Brand + Competitors
      - Stats: Moyenne, Min, Max par entité

3️⃣ PARAMÈTRES DE DATES (OPTIONNELS) ⚠️ TRÈS IMPORTANT!
   
   SCÉNARIO 1: L'utilisateur NE MENTIONNE PAS DE DATES
   ═══════════════════════════════════════════════════
   Exemple user: "Analyse IBIS France" (pas de dates mentionnées)
   → NE PASSE PAS startDate ni endDate
   → Le serveur retourne TOUTES LES DONNÉES DISPONIBLES ✅
   → Comportement: Cherche la plus ancienne jusqu'à aujourd'hui
   → Avantage: Vue complète historique
   
   SCÉNARIO 2: L'utilisateur MENTIONNE UNE PLAGE
   ══════════════════════════════════════════════
   Exemple user: "Données de décembre 2025"
   → Calcule automatiquement:
      startDate="2025-12-01", endDate="2025-12-31"
   → Format: "YYYY-MM-DD" (ex: "2025-12-15")
   
   SCÉNARIO 3: L'utilisateur DEMANDE LES 30 DERNIERS JOURS
   ════════════════════════════════════════════════════════
   Exemple user: "Montre les 30 derniers jours"
   → Calcule automatiquement:
      endDate = aujourd'hui (ex: "2026-02-10")
      startDate = aujourd'hui - 30 jours (ex: "2026-01-11")
   
   SCÉNARIO 4: L'utilisateur DEMANDE TOUT
   ═══════════════════════════════════════
   Exemple user: "Montre-moi tous les données", "Tout depuis le début"
   → NE PASSE PAS startDate ni endDate
   → Retour: Toutes les données disponibles ✅
   
   ⚡ RÈGLE D'OR: Si pas de dates mentionnées → OMETS les paramètres!

4️⃣ PARAMÈTRE MODELS (OPTIONNEL) ⚠️ TRÈS IMPORTANT!
   
   MODÈLES DISPONIBLES (7 au total, tous compatibles):
   ══════════════════════════════════════════════════
   
   Pour analyser les SCORES:
   ├─ "GLOBAL"                    ← Score COMBINÉ (recommandé, défaut)
   │  Utilise: Tous les modèles
   │  Cas d'usage: Analyse générale, comparaison fiable
   │
   ├─ "gpt-5.1"                   ← Model OpenAI GPT-5.1 (nouvelle)
   │  Cas d'usage: Résultats OpenAI, comparaison avec GPT-5
   │
   ├─ "sonar-pro"                 ← Model Perplexity Sonar Pro
   │  Cas d'usage: Résultats Perplexity, recherche avancée
   │
   ├─ "google-ai-overview"        ← Google AI Overview
   │  Cas d'usage: Résultats Google, AI Overview intégré
   │
   ├─ "gpt-interface"             ← GPT Interface (ancienne)
   │  Cas d'usage: Résultats legacy, comparaison historique
   │
   ├─ "gemini-3-pro-preview"      ← Google Gemini 3 Pro Preview
   │  Cas d'usage: Résultats Gemini, preview functionality
   │
   └─ "gpt-5"                     ← Model OpenAI GPT-5 (flagship)
      Cas d'usage: Résultats OpenAI premium, benchmark
   
   SCÉNARIO 1: L'utilisateur NE MENTIONNE PAS DE MODÈLE
   ══════════════════════════════════════════════════════
   Exemple user: "Analyse IBIS France" (pas de modèle mentionné)
   → NE PASSE PAS le paramètre 'models'
   → Le serveur retourne TOUS LES MODÈLES ✅
   → Résultat: Table avec lignes pour chaque (date, model) combo
   → Avantage: Voir l'évolution sur TOUS les modèles
   
   SCÉNARIO 2: L'utilisateur DEMANDE UN SEUL MODÈLE
   ════════════════════════════════════════════════
   Exemple user: "Montre-moi les scores GPT-5.1"
   → Passe: models="gpt-5.1"
   → Résultat: Table filtrée sur GPT-5.1 uniquement
   
   SCÉNARIO 3: L'utilisateur DEMANDE PLUSIEURS MODÈLES
   ═════════════════════════════════════════════════════
   Exemple user: "Compare GPT-5.1, Sonar Pro et Gemini"
   → Passe: models="gpt-5.1,sonar-pro,gemini-3-pro-preview"
   → Format: Séparés par virgules, SANS espaces
   → Résultat: Table avec ces 3 modèles + GLOBAL
   
   SCÉNARIO 4: L'utilisateur DEMANDE LE GLOBAL
   ════════════════════════════════════════════
   Exemple user: "Juste le score global combiné"
   → Passe: models="GLOBAL"
   → Résultat: Table avec GLOBAL uniquement (le plus rapide)
   
   ⚡ RÈGLE D'OR: 
      - Pas de mention → OMETS le paramètre → TOUS les modèles
      - Mentionne un modèle → Passe ce modèle
      - Mentionne plusieurs → Passe tous (séparés par virgule)

5️⃣ COMBINAISON: DATES + MODELS
   
   EXEMPLE COMPLET:
   ════════════════
   User: "Montre-moi le GLOBAL pour les 7 derniers jours"
   
   → Calcule dates:
      endDate = aujourd'hui (ex: "2026-02-10")
      startDate = 7 jours avant (ex: "2026-02-03")
   
   → Filtre modèle:
      models = "GLOBAL"
   
   → Appel:
      get_visibility_scores(
        domainId="...",
        topicId="...",
        startDate="2026-02-03",
        endDate="2026-02-10",
        models="GLOBAL",
        output_format="tabular"
      )

6️⃣ FORMAT DE SORTIE?
   
   Quatre options disponibles:
   ═══════════════════════════
   
   "tabular" (DÉFAUT)
   → Table Markdown lisible + stats auto-calculées
   → Meilleur pour: Analyse humaine, rapports
   → Temps: Normal
   
   "csv"
   → Format CSV pur (copier-coller dans Excel)
   → Meilleur pour: Export Excel/Sheets
   → Temps: Normal
   
   "json"
   → JSON structuré (headers, rows, stats, metadata)
   → Meilleur pour: Intégration code, traitement automatisé
   → Temps: Normal
   
   "stats"
   → Synthèse stats uniquement (Moy/Min/Max par entité)
   → Meilleur pour: Vue rapide, comparaisons
   → Temps: ⚡ 5x plus rapide que tabular

7️⃣ INTERPRÉTER LES RÉSULTATS?
   → Cherche d'abord le RÉSUMÉ PAR ENTITÉ (stats)
   → Puis analyse le DATASET TABULAIRE (tendances)
   → Colonnes = Brand + Competitors (comparer visibilité)
   → Lignes = Dates + Models (voir évolutions)

8️⃣ CAS D'USAGE RAPIDES:
   
   "Analyse complète une région"
   → Omets dates, modèles, format="tabular" (défaut)
   → get_visibility_scores(domainId, topicId)
   
   "Vue rapide (synthèse)"
   → Omets dates, modèles, format="stats"
   → get_visibility_scores(domainId, topicId, output_format="stats")
   
   "Compare GPT-5.1 vs Gemini"
   → models="gpt-5.1,gemini-3-pro-preview"
   → get_visibility_scores(domainId, topicId, models="gpt-5.1,gemini-3-pro-preview")
   
   "7 derniers jours, GLOBAL seulement"
   → models="GLOBAL", calcule dates (-7j)
   → get_visibility_scores(domainId, topicId, startDate="...", endDate="...", models="GLOBAL")
   
   "Export Excel aujourd'hui"
   → output_format="csv"
   → get_visibility_scores(domainId, topicId, output_format="csv")

════════════════════════════════════════════════════════════════════════════════
RÉSUMÉ DES PARAMÈTRES:
════════════════════════════════════════════════════════════════════════════════

PARAMÈTRE      | TYPE        | OPTIONNEL | DÉFAUT           | NOTES
───────────────┼─────────────┼───────────┼──────────────────┼─────────────────
domainId       | string      | ❌ REQUIS | N/A              | De get_domains()
topicId        | string      | ❌ REQUIS | N/A              | De get_domains()
startDate      | YYYY-MM-DD  | ✅ OPT   | Anciennement     | Si omis: tout
endDate        | YYYY-MM-DD  | ✅ OPT   | Aujourd'hui      | Si omis: tout
models         | string      | ✅ OPT   | TOUS les modèles | Séparés par ,
output_format  | string      | ✅ OPT   | "tabular"        | tabular/csv/json/stats

════════════════════════════════════════════════════════════════════════════════
"""

import asyncio
import json
import logging
import os
import sys
from datetime import date, timedelta
from typing import Any
from collections import defaultdict

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

# Modèles disponibles (pour le LLM)
AVAILABLE_MODELS = [
    "GLOBAL",                    # Score combiné (défaut)
    "gpt-5.1",                  # OpenAI GPT-5.1
    "sonar-pro",                # Perplexity Sonar Pro
    "google-ai-overview",       # Google AI Overview
    "gpt-interface",            # GPT Interface
    "gemini-3-pro-preview",     # Google Gemini 3 Pro
    "gpt-5"                     # OpenAI GPT-5
]

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

if not MINT_API_KEY:
    logger.warning("MINT_API_KEY environment variable is missing!")

# Création du serveur MCP
server = Server("mint-visibility-mcp")


# ========== UTILITAIRES POUR DATASET TABULAIRE ==========

def create_tabular_dataset(raw_dataset: list[dict]) -> dict:
    """
    🔧 FONCTION CLÉE: Transforme le dataset brut en PIVOT TABLE structuré
    
    Entrée:
      - raw_dataset: liste de dicts avec Date, EntityName, Score, Model
    
    Sortie:
      - Dataset pivotté avec:
        * headers: ["Date", "Model", "Brand", "Competitor1", "Competitor2", ...]
        * rows: Liste de dicts (une ligne = une date+model)
        * entities: Liste des entités (Brand + Competitors)
        * stats: Statistiques par entité (Moy, Min, Max, Count)
    """
    if not raw_dataset:
        return {"error": "Aucune donnée"}
    
    # Étape 1: Grouper par (date, model)
    pivot_data = {}
    all_entities = set()
    
    for row in raw_dataset:
        date_val = row.get("Date", "")
        model_val = row.get("Model", "")
        entity = row.get("EntityName", "")
        score = row.get("Score", 0)
        
        all_entities.add(entity)
        key = f"{date_val}|{model_val}"
        
        if key not in pivot_data:
            pivot_data[key] = {"Date": date_val, "Model": model_val}
        
        pivot_data[key][entity] = round(score, 2) if isinstance(score, (int, float)) else 0
    
    # Étape 2: Trier les entités (Brand d'abord)
    all_entities = list(all_entities)
    if "Brand" in all_entities:
        all_entities.remove("Brand")
        all_entities = ["Brand"] + sorted(all_entities)
    
    headers = ["Date", "Model"] + all_entities
    
    # Étape 3: Construire les lignes
    rows = []
    for key in sorted(pivot_data.keys()):
        row = {"Date": pivot_data[key]["Date"], "Model": pivot_data[key]["Model"]}
        for entity in all_entities:
            row[entity] = pivot_data[key].get(entity, None)
        rows.append(row)
    
    # Étape 4: Calculer stats
    stats = {}
    for entity in all_entities:
        scores = [r[entity] for r in rows if r[entity] is not None]
        if scores:
            stats[entity] = {
                "average": round(sum(scores) / len(scores), 2),
                "min": round(min(scores), 2),
                "max": round(max(scores), 2),
                "count": len(scores)
            }
    
    return {
        "headers": headers,
        "rows": rows,
        "entities": all_entities,
        "stats": stats,
        "total_rows": len(rows),
        "total_entities": len(all_entities)
    }

def format_as_markdown_table(tabular_data: dict) -> str:
    """📊 Formate le dataset tabulaire en TABLE MARKDOWN lisible"""
    if "error" in tabular_data:
        return f"❌ {tabular_data['error']}"
    
    headers = tabular_data.get("headers", [])
    rows = tabular_data.get("rows", [])
    
    if not rows:
        return "❌ Aucune donnée à afficher"
    
    md = "| " + " | ".join(headers) + " |\n"
    md += "|" + "|".join([":---" for _ in headers]) + "|\n"
    
    for row in rows:
        values = []
        for h in headers:
            val = row.get(h)
            if val is None:
                values.append("-")
            elif isinstance(val, float):
                values.append(f"{val:.2f}%")
            else:
                values.append(str(val))
        md += "| " + " | ".join(values) + " |\n"
    
    return md

def format_as_csv(tabular_data: dict) -> str:
    """📋 Formate le dataset en CSV exploitable"""
    if "error" in tabular_data:
        return f"Error: {tabular_data['error']}"
    
    headers = tabular_data.get("headers", [])
    rows = tabular_data.get("rows", [])
    
    if not rows:
        return "No data"
    
    csv = ",".join(headers) + "\n"
    
    for row in rows:
        values = []
        for h in headers:
            val = row.get(h)
            if val is None:
                values.append("")
            elif isinstance(val, float):
                values.append(f"{val:.2f}")
            else:
                values.append(str(val))
        csv += ",".join(values) + "\n"
    
    return csv

def format_stats_summary(tabular_data: dict) -> str:
    """📊 Génère un RÉSUMÉ DES STATS lisible"""
    if "error" in tabular_data:
        return f"❌ {tabular_data['error']}"
    
    stats = tabular_data.get("stats", {})
    if not stats:
        return "❌ Aucune statistique"
    
    summary = "## 📊 STATISTIQUES PAR ENTITÉ\n\n"
    summary += "| Entité | Moyenne | Min | Max | Mesures |\n"
    summary += "|--------|---------|-----|-----|----------|\n"
    
    for entity in sorted(stats.keys()):
        s = stats[entity]
        summary += f"| {entity} | {s['average']:.2f}% | {s['min']:.2f}% | {s['max']:.2f}% | {s['count']} |\n"
    
    return summary


# ========== LOGIQUE MÉTIER (API & TOOLS) ==========

async def fetch_api(path: str, params: dict = None) -> dict:
    """🔗 Appel API vers Mint.ai"""
    if not MINT_API_KEY:
        raise RuntimeError("MINT_API_KEY environment variable is required")
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{MINT_BASE_URL}{path}", 
            params=params or {}, 
            headers={"X-API-Key": MINT_API_KEY}, 
            timeout=30.0
        )
        response.raise_for_status()
        return response.json()

async def get_domains_and_topics() -> dict:
    """🌍 OUTIL #1: Liste les domaines et topics disponibles"""
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
                all_topics.append({
                    "id": t_id, 
                    "name": t_name, 
                    "domainId": d_id, 
                    "domainName": d_name
                })
                mapping[f"{d_name} > {t_name}"] = {
                    "domainId": d_id, 
                    "topicId": t_id
                }
        except Exception: 
            continue
    
    return {
        "status": "success", 
        "data": {
            "domains": domains, 
            "topics": all_topics, 
            "mapping": mapping
        }
    }

async def get_visibility_scores(
    domainId: str, 
    topicId: str, 
    startDate: str = None, 
    endDate: str = None, 
    models: str = None,
    output_format: str = "tabular"
) -> dict:
    """
    📈 OUTIL #2: Récupère les scores de visibilité en dataset TABULAIRE
    
    ⚠️ PARAMÈTRES OPTIONNELS - TRÈS IMPORTANT POUR LE LLM:
    ═════════════════════════════════════════════════════
    
    startDate (OPTIONNEL):
      - Si OMIS → Retourne TOUTES les données disponibles ✅
      - Si FOURNI → Format "YYYY-MM-DD" (ex: "2025-12-23")
      - Cas courant: User dit "30 derniers jours" → calcule automatiquement
      - Règle: SI user ne mentionne PAS de dates → OMETS ce paramètre
    
    endDate (OPTIONNEL):
      - Si OMIS → Retourne jusqu'à aujourd'hui/présent ✅
      - Si FOURNI → Format "YYYY-MM-DD" (ex: "2026-02-10")
      - Rarement utilisé seul (avec startDate généralement)
      - Règle: SI user ne mentionne PAS de dates → OMETS ce paramètre
    
    models (OPTIONNEL):
      - Si OMIS → Retourne TOUS les modèles disponibles ✅
      - Si FOURNI → Un modèle: "gpt-5.1" ou plusieurs: "gpt-5.1,sonar-pro"
      
      Modèles disponibles:
      ├─ "GLOBAL"                    (défaut, score combiné)
      ├─ "gpt-5.1"                   (OpenAI GPT-5.1)
      ├─ "sonar-pro"                 (Perplexity Sonar Pro)
      ├─ "google-ai-overview"        (Google AI Overview)
      ├─ "gpt-interface"             (GPT Interface)
      ├─ "gemini-3-pro-preview"      (Google Gemini 3 Pro)
      └─ "gpt-5"                     (OpenAI GPT-5)
      
      - User demande "GPT-5.1"? → models="gpt-5.1"
      - User demande "tous"? → OMETS le paramètre
      - User demande "GPT et Sonar"? → models="gpt-5.1,sonar-pro"
      - Règle: SI user ne mentionne PAS de modèle → OMETS ce paramètre
    
    output_format (OPTIONNEL):
      - "tabular" (DÉFAUT): Table Markdown lisible + stats
      - "csv": CSV pur pour Excel
      - "json": JSON structuré
      - "stats": Stats uniquement (5x rapide)
    
    EXEMPLES D'APPELS RÉELS:
    ════════════════════════
    
    User: "Analyse IBIS France" (aucune date, aucun modèle)
    → get_visibility_scores(domainId, topicId)
       (OMETS startDate, endDate, models)
    → Retour: TOUTES les données, TOUS les modèles
    
    User: "30 derniers jours" 
    → Calcule dates: startDate = 30j avant, endDate = aujourd'hui
    → get_visibility_scores(domainId, topicId, startDate="...", endDate="...")
    → Retour: Données derniers 30j, TOUS les modèles
    
    User: "GPT-5.1 uniquement"
    → get_visibility_scores(domainId, topicId, models="gpt-5.1")
    → Retour: Tous les data, FILTRÉS sur GPT-5.1
    
    User: "Décembre 2025, compare GPT-5.1 vs Gemini"
    → Calcule dates: startDate="2025-12-01", endDate="2025-12-31"
    → get_visibility_scores(
        domainId, topicId, 
        startDate="2025-12-01", 
        endDate="2025-12-31",
        models="gpt-5.1,gemini-3-pro-preview"
    )
    → Retour: Données décembre, FILTRÉES sur 2 modèles
    """
    
    # Étape 1: Valider les dates
    # NOTE: Si startDate/endDate sont None, l'API retournera TOUT
    if startDate and endDate:
        # Utilise les dates fournies
        pass
    elif startDate or endDate:
        # Un seul fourni? Accepté par l'API
        pass
    else:
        # AUCUN fourni → API retournera TOUTES les données ✅
        pass
    
    base_params = {
        "latestOnly": "false",
        "page": "1", 
        "limit": "100"
    }
    
    # Ajouter les dates si fournies
    if startDate:
        base_params["startDate"] = startDate
    if endDate:
        base_params["endDate"] = endDate
    
    # Récupération Global
    global_data = await fetch_api(
        f"/domains/{domainId}/topics/{topicId}/visibility/aggregated", 
        base_params
    )
    available_models = global_data.get("availableModels", [])
    
    # Filtre models si spécifié
    models_to_fetch = []
    if models:
        # User a demandé des modèles spécifiques
        models_to_fetch = [m.strip() for m in models.split(",")]
    else:
        # User n'a rien demandé → TOUS les modèles ✅
        models_to_fetch = available_models
    
    # Récupération par modèle
    by_model_data = {}
    for m in models_to_fetch:
        try:
            params = {**base_params, "models": m}
            by_model_data[m] = await fetch_api(
                f"/domains/{domainId}/topics/{topicId}/visibility/aggregated", 
                params
            )
        except: 
            pass

    # Construction dataset brut
    raw_dataset = []
    
    def add_rows(data, model_name):
        """Ajouter les scores au dataset brut"""
        for entry in data.get("chartData", []):
            d = entry.get("date")
            raw_dataset.append({
                "Date": d, 
                "EntityName": "Brand", 
                "EntityType": "Brand", 
                "Score": entry.get("brand"), 
                "Model": model_name
            })
            for c_name, c_score in entry.get("competitors", {}).items():
                raw_dataset.append({
                    "Date": d, 
                    "EntityName": c_name, 
                    "EntityType": "Competitor", 
                    "Score": c_score, 
                    "Model": model_name
                })

    add_rows(global_data, "GLOBAL")
    for m in models_to_fetch: 
        if m in by_model_data:
            add_rows(by_model_data[m], m)

    # Transformer en dataset tabulaire
    tabular = create_tabular_dataset(raw_dataset)
    
    # Retourner selon le format
    if output_format == "csv":
        csv_text = format_as_csv(tabular)
        return {
            "status": "success",
            "format": "csv",
            "output": csv_text,
            "metadata": {
                "total_rows": tabular.get("total_rows", 0),
                "total_entities": tabular.get("total_entities", 0),
                "models_returned": models_to_fetch if models else "ALL",
                "instruction": "Copie ce CSV dans Excel/Google Sheets"
            }
        }
    
    elif output_format == "json":
        return {
            "status": "success",
            "format": "json",
            "output": tabular,
            "metadata": {
                "all_available_models": available_models,
                "models_returned": models_to_fetch if models else "ALL",
                "date_range": f"{startDate or 'all'} to {endDate or 'all'}",
                "instruction": "Utilise ce JSON pour traitement automatisé"
            }
        }
    
    elif output_format == "stats":
        stats_text = format_stats_summary(tabular)
        return {
            "status": "success",
            "format": "stats",
            "output": stats_text,
            "metadata": tabular.get("stats"),
            "instruction": "Ces stats permettent une analyse rapide"
        }
    
    else:  # "tabular" (défaut)
        markdown_text = format_as_markdown_table(tabular)
        stats_text = format_stats_summary(tabular)
        full_output = f"{stats_text}\n\n## 📋 DATASET TABULAIRE\n\n{markdown_text}"
        
        return {
            "status": "success",
            "format": "tabular",
            "output": full_output,
            "metadata": {
                "total_rows": tabular.get("total_rows", 0),
                "total_entities": tabular.get("total_entities", 0),
                "entities": tabular.get("entities", []),
                "date_range": f"{startDate or 'all'} to {endDate or 'all'}",
                "all_available_models": available_models,
                "models_returned": models_to_fetch if models else "ALL",
                "instruction": "Analyse les stats + le tableau pour conclusions"
            }
        }


@server.list_tools()
async def list_tools() -> list[Tool]:
    """📋 Liste les outils disponibles"""
    return [
        Tool(
            name="get_domains_and_topics",
            description="🌍 COMMENCER PAR LÀ: Liste domaines et topics avec IDs. Utilise cet outil en premier pour trouver les domainId/topicId corrects!",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="get_visibility_scores",
            description="📈 ANALYSE: Dataset TABULAIRE (lignes=Date+Model, colonnes=Brand+Competitors). Formats: 'tabular' (défaut), 'csv' (Excel), 'json', 'stats' (rapide). ⚠️ PARAMÈTRES OPTIONNELS: startDate/endDate (si omis → toutes les données), models (si omis → tous: GLOBAL, gpt-5.1, sonar-pro, google-ai-overview, gpt-interface, gemini-3-pro-preview, gpt-5)",
            inputSchema={
                "type": "object",
                "properties": {
                    "domainId": {
                        "type": "string",
                        "description": "ID du domaine (REQUIS, obtenu de get_domains_and_topics)"
                    },
                    "topicId": {
                        "type": "string",
                        "description": "ID du topic (REQUIS, obtenu de get_domains_and_topics)"
                    },
                    "startDate": {
                        "type": "string",
                        "description": "⚠️ OPTIONNEL: Format YYYY-MM-DD (ex: 2025-12-23). SI OMIS → toutes les données! Ne l'utilise que si user mentionne une date de début."
                    },
                    "endDate": {
                        "type": "string",
                        "description": "⚠️ OPTIONNEL: Format YYYY-MM-DD (ex: 2026-02-10). SI OMIS → jusqu'à aujourd'hui/présent! Ne l'utilise que si user mentionne une date de fin."
                    },
                    "models": {
                        "type": "string",
                        "description": "⚠️ OPTIONNEL: Modèles à filtrer. SI OMIS → TOUS les modèles! Disponibles: GLOBAL, gpt-5.1, sonar-pro, google-ai-overview, gpt-interface, gemini-3-pro-preview, gpt-5. Format: 'gpt-5.1' ou 'gpt-5.1,sonar-pro' (séparés par virgule, sans espaces). Ne l'utilise que si user demande un modèle spécifique."
                    },
                    "output_format": {
                        "type": "string",
                        "enum": ["tabular", "csv", "json", "stats"],
                        "description": "Format sortie: 'tabular' (DÉFAUT, table+stats) | 'csv' (pour Excel) | 'json' (pour code) | 'stats' (synthèse rapide, 5x plus rapide)"
                    }
                },
                "required": ["domainId", "topicId"]
            }
        )
    ]

@server.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    """🔧 Exécute un outil"""
    try:
        if name == "get_domains_and_topics":
            res = await get_domains_and_topics()
        elif name == "get_visibility_scores":
            res = await get_visibility_scores(**arguments)
        else:
            raise ValueError(f"Unknown tool: {name}")
        
        output = res.get("output", "")
        if isinstance(output, str):
            return [TextContent(type="text", text=output)]
        else:
            return [TextContent(type="text", text=json.dumps(res, indent=2, default=str))]
    
    except Exception as e:
        return [TextContent(type="text", text=f"❌ Erreur: {str(e)}")]


# ========== CONFIGURATION WEB (SSE) ==========

sse = SseServerTransport("/messages")

async def handle_sse_connect(request: Request):
    """Gère la connexion SSE (GET)"""
    async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
        await server.run(streams[0], streams[1], server.create_initialization_options())

async def handle_messages(request: Request):
    """Gère les messages (POST)"""
    await sse.handle_post_message(request.scope, request.receive, request._send)

routes = [
    Route("/sse", endpoint=handle_sse_connect, methods=["GET"]),
    Route("/sse", endpoint=handle_messages, methods=["POST"]),
    Route("/messages", endpoint=handle_messages, methods=["POST"])
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
