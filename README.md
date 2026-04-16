# 🚀 Mint.ai Visibility MCP Server

Serveur MCP pour analyser la visibilité de marque dans les LLMs via l'API Mint.ai.

**Version 4.0.0** — Classification 2 axes des sources (ownership + brand_status), courbe temporelle binnée, et couplage strict (reportId, url) pour l'enrichment.

---

## 🛠️ Tools disponibles (7)

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
  },
  "errors": []
}
```

---

### 2. `get_topic_scores`

Scores de visibilité Brand + Competitors, par modèle LLM, sur une période donnée.

À utiliser pour **zoomer sur UN topic précis** : historique jour par jour, Brand vs Concurrents, décomposition par modèle.

**Paramètres :**
| Paramètre | Requis | Description |
|-----------|--------|-------------|
| `domainId` | ✅ | ID du domaine |
| `topicId` | ✅ | ID du topic |
| `startDate` | optionnel | Date début YYYY-MM-DD (défaut : -30 jours) |
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
      { "Date": "2026-04-10", "EntityName": "Brand", "EntityType": "Brand", "Score": 64.14, "Model": "GLOBAL" },
      { "Date": "2026-04-10", "EntityName": "Competitor A", "EntityType": "Competitor", "Score": 44.82, "Model": "gpt-5.1" }
    ],
    "metadata": {
      "models": ["GLOBAL", "gpt-5.1", "sonar-pro", "gemini-3-pro-preview"]
    }
  }
}
```

---

### 3. `get_scores_overview`

Tableau synthétique des scores moyens de visibilité pour **PLUSIEURS topics en un seul appel**.

Le tool est **autonome** : il récupère lui-même tous les topics disponibles, boucle en parallèle côté serveur (semaphore globale à 8 requêtes simultanées), et retourne un tableau Markdown compact avec le score moyen par topic — sans historique, sans concurrents, sans décomposition par modèle.

Utile pour :
- Vue comparative rapide multi-topics / multi-brands sur une période
- Synthèse globale (ex: tous les marchés IBIS sur janvier)
- Identifier les topics les plus et moins performants

> ⚠️ Ne pas utiliser pour analyser Brand vs Concurrents ou l'historique détaillé → utiliser `get_topic_scores` à la place.

**Paramètres :**
| Paramètre | Requis | Description |
|-----------|--------|-------------|
| `startDate` | optionnel | Date début YYYY-MM-DD (défaut : -90 jours) |
| `endDate` | optionnel | Date fin YYYY-MM-DD (défaut : aujourd'hui) |
| `models` | optionnel | Filtre modèles séparés par virgule (défaut : cross-modèles) |
| `brand_filter` | optionnel | Filtrer par brand (ex: `IBIS`, `Mercure`) |
| `market_filter` | optionnel | Filtrer par marché dans le nom du topic (ex: `FR`, `UK`) |
| `topic_ids` | optionnel | Liste explicite de topicIds |

**Exemples d'utilisation :**
```
# Tous les topics, 90 derniers jours
{}

# Tous les marchés IBIS sur janvier 2026
{ "brand_filter": "IBIS", "startDate": "2026-01-01", "endDate": "2026-01-31" }
```

**Retour — exemple de tableau Markdown :**
```
## 📊 Scores moyens — 2026-01-01 → 2026-01-31
*36 topics*

| Brand    | Topic       | Score moy. | N reports | Statut |
|----------|-------------|:----------:|:---------:|--------|
| Fairmont | Fairmont FR | **67.4**   | 13        | 🟢     |
| IBIS     | IBIS FR     | **57.2**   | 12        | 🟡     |
|          | IBIS UK     | **61.4**   | 11        | 🟢     |
```

---

### 4. `get_visibility_trend` *(nouveau v4)*

**Série temporelle binnée** (jour/semaine/mois) des scores de visibilité, prête à être affichée en line chart.

Quand tu reçois ce retour, tu dois **générer un graphique** dans un artifact Claude (Recharts, etc.).

**Paramètres :**
| Paramètre | Requis | Description |
|-----------|--------|-------------|
| `brand_filter` / `market_filter` / `topic_ids` | optionnel | Sélection des topics |
| `startDate` / `endDate` | optionnel | Défaut : depuis le 1er janvier de l'année courante |
| `models` | optionnel | Filtre modèles |
| `granularity` | optionnel | `"day"` / `"week"` (défaut) / `"month"` |
| `aggregation` | optionnel | `"average"` (défaut, 1 série moyennée) / `"per_topic"` (1 série par topic) |

**Retour :**
```json
{
  "status": "success",
  "series": [
    {
      "name": "IBIS (week avg)",
      "points": [
        { "date": "2026-01-06", "score": 52.3, "n": 45 },
        { "date": "2026-01-13", "score": 55.1, "n": 48 }
      ]
    }
  ],
  "chart_hint": "line"
}
```

---

### 5. `get_topic_sources`

Top domaines et URLs cités par les LLMs dans leurs réponses, par modèle. Utilise l'API `aggregated?includeDetailedResults=true` (agrégation côté Mint, plus rapide que reconstruction depuis les raw results).

Effectue **1 call GLOBAL + 1 call par modèle en parallèle** (`asyncio.gather`), ce qui permet de comparer quels domaines/URLs sont cités selon le modèle (GPT-5 cite-t-il les mêmes sources que Gemini ?).

Utile pour :
- Identifier quels sites sont les plus cités dans les réponses LLM
- Comparer les sources entre modèles
- Analyser l'évolution des citations dans le temps

**Paramètres :**
| Paramètre | Requis | Description |
|-----------|--------|-------------|
| `domainId` | ✅ | ID du domaine |
| `topicId` | ✅ | ID du topic |
| `startDate` | optionnel | Date début (défaut : -90 jours) |
| `endDate` | optionnel | Date fin (défaut : aujourd'hui) |
| `models` | optionnel | Filtre modèles |

**Retour :**
```json
{
  "status": "success",
  "data": {
    "top_domains": [
      { "Model": "GLOBAL",   "Domain": "booking.com",     "CitationCount": 142, "Rank": 1 },
      { "Model": "gpt-5.1",  "Domain": "booking.com",     "CitationCount": 87,  "Rank": 1 },
      { "Model": "sonar-pro","Domain": "tripadvisor.com", "CitationCount": 54,  "Rank": 1 }
    ],
    "top_urls": [...],
    "domains_over_time": [...],
    "global_metrics": [
      { "Model": "GLOBAL", "TotalPrompts": 320, "TotalAnswers": 1280, "TotalCitations": 4200, "ReportCount": 8 }
    ]
  }
}
```

---

### 6. `get_raw_responses` *(cœur v4)* 🎯

**Tool cœur** pour toutes les analyses fines de sources. Classifie chaque URL citée sur **2 axes indépendants** :

#### Axe 1 — `ownership` (via regex sur domaine)
- `owned` = URL sur un domaine propriétaire (ex: `all.accor.com` pour IBIS)
- `external` = URL sur un domaine tiers (booking.com, tripadvisor.com...)

#### Axe 2 — `brand_status` (via API `/sources/enrichment`)
- `own_only` = page mentionne ta marque uniquement
- `own+comp` = page mentionne ta marque **et** des concurrents
- `comp_only` = page mentionne uniquement des concurrents
- `no_brand` = crawl a tourné mais rien détecté textuellement
- `not_enriched` = pas de crawl disponible

**3 modes via `aggregate` :**
- `"classified"` (défaut) — enrichment + classification complète + matrice croisée
- `"sources"` — top domaines/URLs sans enrichment (plus rapide)
- `"none"` — responses brutes pour drill-down

**Paramètres clés :**
| Paramètre | Requis | Description |
|-----------|--------|-------------|
| `domainId` | ✅ | ID du domaine |
| `topic_ids` / `brand_filter` / `market_filter` | optionnel | Sélection topics |
| `response_brand_mentioned` | optionnel | `"true"` / `"false"` / `"all"` — filtre **niveau réponse LLM** |
| `ownership_filter` | optionnel | Axe 1 : `"owned"` / `"external"` / `"all"` |
| `brand_status_filter` | optionnel | Axe 2 : string ou liste parmi les 5 statuts |
| `aggregate` | optionnel | Mode (défaut `"classified"`) |
| `startDate` / `endDate` / `models` / `latestOnly` | optionnel | — |
| `top_n` | optionnel | Nombre max URLs (défaut 30) |

**Exemples d'utilisation :**

```python
# "Sources qui citent mes concurrents mais pas moi, quand IBIS est mentionné"
{
  "domainId": "...",
  "topic_ids": ["..."],
  "response_brand_mentioned": "true",
  "brand_status_filter": "comp_only",
  "ownership_filter": "external"
}

# "Sources externes qui mentionnent ma marque"
{
  "ownership_filter": "external",
  "brand_status_filter": ["own_only", "own+comp"]
}

# "Quand IBIS n'est PAS cité, qui prend ma place ?"
{
  "response_brand_mentioned": "false",
  "aggregate": "sources"
}
```

**Retour (mode classified) :**
```json
{
  "status": "success",
  "classified_urls": [
    {
      "url": "https://booking.com/ibis-paris",
      "domain": "booking.com",
      "ownership": "external",
      "brand_status": "own+comp",
      "own_brands": ["IBIS"],
      "comp_brands": ["Hilton"],
      "own_count": 5,
      "comp_count": 2,
      "category": "Travel & Tourism > Booking Services",
      "couples": "1/1"
    }
  ],
  "matrix": [
    { "ownership": "owned",    "own_only": 45, "own+comp": 12, "comp_only": 0,   "no_brand": 180, "not_enriched": 15, "TOTAL": 252 },
    { "ownership": "external", "own_only": 32, "own+comp": 28, "comp_only": 120, "no_brand": 50,  "not_enriched": 200, "TOTAL": 430 }
  ],
  "metadata": {
    "couples_enriched": 456,
    "couples_total": 501,
    "brand_name": "IBIS"
  }
}
```

> 🔒 **Couplage `(reportId, url)` respecté strictement** : chaque URL est enrichie avec SON reportId propre (pas de dédup cross-report). C'est le contract de l'API Mint — bug de la v3.x corrigé en v4.

---

### 7. `enrich_sources` *(nouveau v4)*

Accès direct à l'endpoint `/sources/enrichment`. Pour une liste d'URLs + un `reportId`, retourne la catégorie DataForSEO et les brands détectées (isBrand=true → own, isBrand=false → competitor).

Chunks automatiques à 100 URLs (limite API).

**Paramètres :**
| Paramètre | Requis | Description |
|-----------|--------|-------------|
| `domainId` | ✅ | ID du domaine |
| `urls` | ✅ | Liste d'URLs à enrichir |
| `reportId` | ✅ | Report ID source |
| `topicId` | optionnel | Résout market/langue |
| `brand_name` | optionnel | Si fourni, ajoute classification owned/external |

> ⚠️ Pour l'analyse typique, utiliser plutôt `get_raw_responses(aggregate="classified")` qui automatise tout le flow.

---

## 📦 Installation

```bash
pip install -r requirements.txt
```

## 🚀 Lancement local

```bash
export MINT_API_KEY="mint_live_your_key_here"
uvicorn mcp_mint_server:app --host 0.0.0.0 --port 8000 --workers 1
```

Endpoints exposés :
- `GET /health` — healthcheck JSON
- `GET /sse` — connexion SSE (Claude.ai, clients web MCP)
- `POST /sse` + `POST /messages` — messages JSON-RPC

## 📊 Configuration Claude Desktop

```json
{
  "mcpServers": {
    "mint-visibility": {
      "command": "python",
      "args": ["-m", "uvicorn", "mcp_mint_server:app", "--port", "8765"],
      "cwd": "/absolute/path/to/mint-mcp-v4",
      "env": {
        "MINT_API_KEY": "mint_live_your_key_here"
      }
    }
  }
}
```

## ☁️ Déploiement Render

1. Push le repo sur GitHub
2. Sur Render → New Web Service → connecter le repo
3. Build command : `pip install -r requirements.txt`
4. Start command : `uvicorn mcp_mint_server:app --host 0.0.0.0 --port $PORT --workers 1`
5. Renseigner `MINT_API_KEY` dans Environment
6. Deploy

Puis dans Claude.ai : connecter sur `https://<ton-app>.onrender.com/sse`.

## 📁 Structure du projet

```
.
├── mcp_mint_server.py   # Serveur MCP complet (tout-en-un)
├── owned_domains.json   # Mapping brand → domaines propriétaires
├── requirements.txt     # Dépendances
├── .gitignore
└── README.md
```

## 🏠 Configuration "Owned Domains"

Le fichier `owned_domains.json` mappe chaque brand à ses domaines propriétaires.

**Pourquoi ?** L'API Mint ne crawle que les pages owned + concurrents. Pour une URL `all.accor.com` sans mention textuelle détectée par le crawl, elle est quand même **ta propriété éditoriale**. Le signal `ownership='owned'` le capture indépendamment du crawl.

**Ajouter une nouvelle brand** : édite `owned_domains.json` et redémarre le serveur. Le fallback `_default` couvre les brands non listées.

Exemple :
```json
{
  "IBIS":     ["accor.com"],
  "Fairmont": ["accor.com", "fairmont.com"],
  "_default": ["accor.com"]
}
```

## 🔑 Variables d'environnement

| Variable | Requis | Défaut | Description |
|----------|--------|--------|-------------|
| `MINT_API_KEY` | ✅ | — | Clé API Mint.ai |
| `MINT_BASE_URL` | optionnel | `https://api.getmint.ai/api` | URL de base |
| `HTTP_TIMEOUT` | optionnel | `30.0` | Timeout HTTP (secondes) |
| `HTTP_MAX_CONCURRENT` | optionnel | `8` | Concurrence max API |
| `OWNED_DOMAINS_PATH` | optionnel | `./owned_domains.json` | Chemin mapping |

## 📄 Changelog

### v4.0.0 (2026-04-16) — Sources Classification

**Refonte majeure.** Non-rétrocompatible avec v3.x.

- ✅ Passage de 4 à 7 tools, plus spécialisés
- ✅ **Classification 2 axes indépendants** (ownership + brand_status)
- ✅ Nouveau tool `get_visibility_trend` pour graphiques temporels
- ✅ Nouveau tool `get_raw_responses` (cœur v4) avec 3 modes
- ✅ Nouveau tool `enrich_sources` accès direct enrichment
- ✅ Fichier `owned_domains.json` externe pour mapping brand → domaines
- ✅ Couplage `(reportId, url)` respecté strictement (fix bug v3.x)
- ✅ Erreurs typées remontées au client (auth/not_found/rate_limit/api)
- ✅ Retry exponentiel auto sur 429/5xx avec respect de `X-RateLimit-Reset`
- ✅ Semaphore globale pour limiter la concurrence API
- ✅ Endpoint `/health` pour healthchecks plateformes

**Mapping v3.6 → v4 :**
- `get_visibility_scores` → `get_topic_scores`
- `get_visibility_monthly_summary` → `get_scores_overview`
- `get_citations` → `get_topic_sources` + `get_raw_responses` (plus complet)

### v3.6.0 (2026-02-23)
- ✅ Tool `get_visibility_monthly_summary` : tableau multi-topics côté serveur

### v3.5.0 (2026-02-20)
- ✅ Tool `get_citations` : top domaines & URLs

### v3.4.0 (2026-02-09)
- ✅ Historique 365 jours, limit 1000, fix 405 sur `/sse`

### v3.0.0 (2026-01-15)
- ✅ Tools `get_domains_and_topics`, `get_visibility_scores`
