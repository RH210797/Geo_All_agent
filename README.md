# 🚀 Mint.ai Visibility MCP Server

Serveur MCP pour analyser la visibilité de marque dans les LLMs via l'API Mint.ai

**Version 3.6.0** - Visibility Monthly Summary (tableau multi-topics)

---

## 🛠️ Tools disponibles (4)

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

À utiliser pour **zoomer sur UN topic précis** : historique jour par jour, Brand vs Concurrents, décomposition par modèle.

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
- Analyser l'évolution des citations dans le temps

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
      {"Model": "GLOBAL",   "Domain": "booking.com",     "CitationCount": 142, "Rank": 1},
      {"Model": "gpt-5",    "Domain": "booking.com",     "CitationCount": 87,  "Rank": 1},
      {"Model": "sonar-pro","Domain": "tripadvisor.com", "CitationCount": 54,  "Rank": 1}
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

### 4. `get_visibility_monthly_summary`

Tableau synthétique des scores moyens de visibilité pour **PLUSIEURS topics en un seul appel**.

Le tool est **autonome** : il récupère lui-même tous les topics disponibles via `get_domains_and_topics`, boucle côté serveur (1 call API par topic en parallèle), et retourne un tableau Markdown compact avec le score moyen par topic — sans historique, sans concurrents, sans décomposition par modèle.

Utile pour :
- Vue comparative rapide multi-topics / multi-brands sur une période
- Synthèse globale de la visibilité (ex: tous les marchés IBIS sur janvier)
- Identifier les topics les plus et moins performants

> ⚠️ Ne pas utiliser pour analyser Brand vs Concurrents ou l'historique détaillé → utiliser `get_visibility_scores` à la place.

**Paramètres :**
| Paramètre | Requis | Description |
|-----------|--------|-------------|
| `startDate` | optionnel | Date début YYYY-MM-DD (défaut : -90 jours) |
| `endDate` | optionnel | Date fin YYYY-MM-DD (défaut : aujourd'hui) |
| `models` | optionnel | Filtre modèles séparés par virgule (défaut : cross-modèles) |
| `brand_filter` | optionnel | Filtrer par brand (ex: `IBIS`, `Mercure`) |
| `market_filter` | optionnel | Filtrer par marché dans le nom du topic (ex: `FR`, `UK`) |

> **Note sur les filtres :** `brand_filter` et `market_filter` filtrent la liste des topics avant de faire les calls API, ce qui réduit le nombre de requêtes (ex: `brand_filter="IBIS"` → 5 calls au lieu de 36).

**Exemples d'utilisation :**
```
# Tous les topics, 90 derniers jours
{}

# Tous les marchés IBIS sur janvier 2026
{ "brand_filter": "IBIS", "startDate": "2026-01-01", "endDate": "2026-01-31" }

# Tous les marchés FR, sur Sonar Pro uniquement
{ "market_filter": "FR", "models": "sonar-pro" }
```

**Retour — exemple de tableau Markdown :**
```
## 📊 Scores moyens — 2026-01-01 → 2026-01-31
*36 topics | modèles: all (cross-models)*

| Brand    | Topic       | Score moy. | N reports | Statut |
|----------|-------------|:----------:|:---------:|--------|
| Fairmont | Fairmont FR | **67.4**   | 13        | 🟢     |
|          | Fairmont UK | **59.3**   | 12        | 🟡     |
| IBIS     | IBIS FR     | **57.2**   | 12        | 🟡     |
|          | IBIS UK     | **61.4**   | 11        | 🟢     |
|          | IBIS DE     | **49.8**   | 10        | 🟡     |
|          | IBIS AU     | **42.3**   | 9         | 🟡     |
|          | IBIS BR     | **31.5**   | 8         | 🟠     |

---
Moyenne globale : 52.3 | Meilleur : Fairmont FR (67.4) | Plus bas : IBIS BR (31.5)
_🟢 ≥60 | 🟡 40–59 | 🟠 20–39 | 🔴 <20 | ⚠️ no data_
```

**Retour JSON :**
```json
{
  "status": "success",
  "markdown_table": "## 📊 Scores moyens ...",
  "rows": [
    {"brand": "IBIS", "topic": "IBIS FR", "avg_score": 57.2, "data_points": 12, "error": null}
  ],
  "metadata": {
    "startDate": "2026-01-01",
    "endDate": "2026-01-31",
    "models": "all (cross-models)",
    "topic_count": 36
  }
}
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

## 🔑 Variables d'environnement

| Variable | Requis | Description |
|----------|--------|-------------|
| `MINT_API_KEY` | ✅ | Clé API Mint.ai |
| `MINT_BASE_URL` | optionnel | URL de base (défaut : `https://api.getmint.ai/api`) |

## 📄 Changelog

### v3.6.0 (2026-02-23)
- ✅ Tool `get_visibility_monthly_summary` : tableau multi-topics côté serveur
- ✅ Itération autonome sur tous les topics via `get_domains_and_topics`
- ✅ Batches de 8 appels parallèles (`asyncio.gather`) pour minimiser la latence
- ✅ Filtres optionnels `brand_filter` et `market_filter`
- ✅ Retour Markdown compact — économise les tokens vs appels multiples

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