# 🚀 Mint.ai Visibility MCP Server

Serveur MCP pour analyser la visibilité de marque dans les LLMs via l'API Mint.ai

**Version 3.0.0** - Dataset structuré complet

## 🛠️ Tools disponibles (2)

### 1. `get_domains_and_topics`

Liste TOUS les domaines et topics disponibles.

**Utilisation** :
- "Quels domaines j'ai ?"
- "Liste mes topics"

**Retour** :
```json
{
  "domains": [...],
  "topics": [...],
  "mapping": {
    "Fairmont > Fairmont US": {
      "domainId": "694a86...",
      "topicId": "694a86..."
    }
  },
  "summary": {
    "totalDomains": 5,
    "totalTopics": 15
  }
}
```

### 2. `get_visibility_scores`

Analyse COMPLÈTE avec dataset structuré (Brand + Competitors par modèle).

**Format du dataset** :
```
Date | EntityName | EntityType | Score | Model | Variation_Points | Variation_Percent
```

**Paramètres** :
- `domainId` (requis) - ID du domaine
- `topicId` (requis) - ID du topic
- `startDate` (optionnel) - Date début YYYY-MM-DD
- `endDate` (optionnel) - Date fin YYYY-MM-DD
- `models` (optionnel) - Filtre modèles

**Retour** :
```json
{
  "status": "success",
  "data": {
    "dataset": [
      {
        "Date": "2025-12-23",
        "EntityName": "Your Brand",
        "EntityType": "Brand",
        "Score": 64.14,
        "Model": "GLOBAL",
        "Variation_Points": null,
        "Variation_Percent": null
      },
      {
        "Date": "2025-12-23",
        "EntityName": "Four Seasons",
        "EntityType": "Competitor",
        "Score": 44.82,
        "Model": "GLOBAL",
        "Variation_Points": null,
        "Variation_Percent": null
      }
    ],
    "metadata": {
      "totalRows": 150,
      "brandRows": 42,
      "competitorRows": 108,
      "uniqueCompetitors": 5,
      "modelsAnalyzed": 7,
      "models": ["GLOBAL", "gpt-5.1", "gemini-3-pro-preview", ...]
    }
  }
}
```

## 📦 Installation

```bash
pip install -r requirements.txt
```

## 🚀 Lancement

```bash
export MINT_API_KEY="mint_live_your_key_here"
python mcp_mint_server.py
```

## 📊 Configuration Claude Desktop

Ajouter dans `claude_desktop_config.json` :

```json
{
  "mcpServers": {
    "mint-visibility": {
      "command": "python",
      "args": ["/absolute/path/to/mcp_mint_server.py"],
      "env": {
        "MINT_API_KEY": "mint_live_your_key_here"
      }
    }
  }
}
```

## 🧪 Test

```bash
# Installation
pip install -r requirements.txt

# Configuration
export MINT_API_KEY="mint_live_..."

# Lancement
python mcp_mint_server.py
```

## 📁 Structure du projet

```
.
├── mcp_mint_server.py   # Serveur MCP principal
├── requirements.txt     # Dépendances
└── README.md           # Cette documentation
```

## 🔄 Changelog

### v3.0.0 (2026-02-09)
- ✅ Tool `get_domains_and_topics` : Liste domaines et topics
- ✅ Tool `get_visibility_scores` : Dataset structuré complet
- ✅ Format : Date | EntityName | EntityType | Score | Model | Variation
- ✅ Support split par modèle LLM automatique
- ✅ Brand + Competitors avec évolutions

## 📊 Format du dataset

**Colonnes** :
1. `Date` - Date de la période (YYYY-MM-DD)
2. `EntityName` - Nom de l'entité (Brand ou Competitor)
3. `EntityType` - Type ("Brand" ou "Competitor")
4. `Score` - Score de visibilité (0-100)
5. `Model` - Modèle LLM ("GLOBAL" ou nom du modèle)
6. `Variation_Points` - Évolution en points vs période précédente
7. `Variation_Percent` - Évolution en % vs période précédente

**Exemple d'utilisation** :
- Analyser l'évolution de la marque sur GPT-5
- Comparer Brand vs Competitors
- Voir la tendance globale (GLOBAL)
- Identifier les modèles où on performe le mieux

## 🆘 Support

Variables d'environnement requises :
- `MINT_API_KEY` : Votre clé API Mint.ai

Variables optionnelles :
- `MINT_BASE_URL` : URL de l'API (défaut: https://api.getmint.ai/api)
