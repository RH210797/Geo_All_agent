# 🚀 Mint.ai Visibility MCP Server

Serveur MCP pour analyser la visibilité de marque dans les LLMs via l'API Mint.ai

**Version 3.5.0** - Top Domains & URLs par modèle LLM

---

## 🛠️ Tools disponibles (3)

---

### 1. `get_domains_and_topics`

Liste tous les domaines et topics disponibles. **À utiliser en premier** pour récupérer les IDs nécessaires aux autres tools.

**Exemples d'utilisation :**
- "Quels domaines j'ai ?"
- "Liste mes topics"

**Retour :**
```json
{
  "domains": [...],
  "topics": [...],
  "mapping": {
    "IBIS > IBIS FR": {
      "domainId": "694a...",
      "topicId": "694a..."
    }
  }
}
```

---

### 2. `get_visibility_scores`

Scores de visibilité Brand + Competitors, par modèle LLM, sur une période donnée.

**Paramètres :**
| Paramètre | Requis | Description |
|-----------|--------|-------------|
| `domainId` | ✅ | ID du domaine |
| `topicId` | ✅ | ID du topic |
| `startDate` | optionnel | Date début YYYY-MM-DD (défaut : -365 jours) |
| `endDate` | optionnel | Date fin YYYY-MM-DD (défaut : aujourd'hui) |
| `models` | optionnel | Filtre modèles séparés par virgule |

**Format du dataset retourné :**
```
Date | EntityName | EntityType | Score | Model
```

**Retour :**
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
        "Model": "GLOBAL"
      },
      {
        "Date": "2025-12-23",
        "EntityName": "Competitor A",
        "EntityType": "Competitor",
        "Score": 44.82,
        "Model": "gpt-5"
      }
    ],
    "metadata": {
      "models": ["GLOBAL", "gpt-5", "gemini-3-pro-preview", "sonar-pro", "gpt-interface"]
    }
  }
}
```

---

### 3. `get_citations`

Top domaines et top URLs cités par les LLMs dans leurs réponses, par modèle.

Effectue **1 call GLOBAL + 1 call par modèle disponible en parallèle** (`asyncio.gather`), ce qui permet de comparer quels domaines/URLs sont cités selon le modèle (GPT-5 cite-t-il les mêmes sources que Gemini ?).

Utile pour :
- Identifier quels sites sont les plus cités dans les réponses LLM
- Comparer les sources entre modèles (gpt-interface vs sonar-pro vs gemini)
- Analyser l'évolution des citations dans le temps (passer deux périodes différentes)

**Paramètres :**
| Paramètre | Requis | Description |
|-----------|--------|-------------|
| `domainId` | ✅ | ID du domaine |
| `topicId` | ✅ | ID du topic |
| `startDate` | optionnel | Date début YYYY-MM-DD (défaut : -90 jours) |
| `endDate` | optionnel | Date fin YYYY-MM-DD (défaut : aujourd'hui) |
| `models` | optionnel | Filtre modèles séparés par virgule (défaut : tous) |

**Retour :**
```json
{
  "status": "success",
  "data": {
    "top_domains": [
      {"Model": "GLOBAL",  "Domain": "booking.com",  "CitationCount": 142, "Rank": 1},
      {"Model": "gpt-5",   "Domain": "booking.com",  "CitationCount": 87,  "Rank": 1},
      {"Model": "sonar-pro","Domain": "tripadvisor.com","CitationCount": 54,"Rank": 1}
    ],
    "top_urls": [
      {"Model": "GLOBAL", "Url": "https://booking.com/...", "Domain": "booking.com", "CitationCount": 23, "Rank": 1}
    ],
    "domains_over_time": [
      {"Model": "GLOBAL", "Date": "2026-01-15", "Domain": "booking.com", "Count": 12}
    ],
    "urls_over_time": [...],
    "global_metrics": [
      {"Model": "GLOBAL", "TotalPrompts": 320, "TotalAnswers": 1280, "TotalCitations": 4200, "ReportCount": 8}
    ],
    "metadata": {
      "models": ["GLOBAL", "gpt-5", "sonar-pro", "gemini-3-pro-preview", "gpt-interface"],
      "startDate": "2026-01-01",
      "endDate": "2026-01-31"
    }
  }
}
```

**Exemple — comparer deux périodes :**
```
→ Appel 1 : startDate="2026-01-01" endDate="2026-01-15"
→ Appel 2 : startDate="2026-01-16" endDate="2026-01-31"
→ Comparer top_domains entre les deux résultats
```

---

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

## 📁 Structure du projet

```
.
├── mcp_mint_server.py   # Serveur MCP principal
├── requirements.txt     # Dépendances
└── README.md            # Cette documentation
```

## 📄 Changelog

### v3.5.0 (2026-02-20)
- ✅ Tool `get_citations` : top domaines & URLs cités par les LLMs
- ✅ 1 call GLOBAL + appels parallèles par modèle (`asyncio.gather`)
- ✅ Retourne : top_domains, top_urls, domains_over_time, urls_over_time, global_metrics
- ✅ Comparaison inter-modèles (gpt-interface vs sonar-pro vs gemini)
- ✅ Comparaison temporelle via startDate/endDate

### v3.4.0 (2026-02-09)
- ✅ Extension historique par défaut à 365 jours
- ✅ Limite de résultats augmentée à 1000 entrées
- ✅ Correction erreur 405 sur `/sse`

### v3.0.0 (2026-01-15)
- ✅ Tool `get_domains_and_topics`
- ✅ Tool `get_visibility_scores` avec dataset structuré
- ✅ Support split par modèle LLM automatique

## 🔑 Variables d'environnement

| Variable | Requis | Description |
|----------|--------|-------------|
| `MINT_API_KEY` | ✅ | Clé API Mint.ai |
| `MINT_BASE_URL` | optionnel | URL de base (défaut : `https://api.getmint.ai/api`) |
