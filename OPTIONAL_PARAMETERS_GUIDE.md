# 🎯 Guide des Paramètres Optionnels - MCP v3.5.2

## ⚠️ RÈGLE D'OR

```
SI l'utilisateur NE MENTIONNE PAS → OMETS le paramètre
SI l'utilisateur DEMANDE → PASSE le paramètre
```

---

## 📅 Paramètre startDate/endDate

### ❌ MAUVAIS:
```python
User: "Analyse IBIS France"
↓
LLM: get_visibility_scores(
    domainId="...",
    topicId="...",
    startDate="2025-12-01",     # ← MAUVAIS! User n'a pas demandé
    endDate="2026-02-10"        # ← MAUVAIS! Limite arbitraire
)
```

### ✅ BON:
```python
User: "Analyse IBIS France"
↓
LLM: get_visibility_scores(
    domainId="...",
    topicId="..."
    # ← OMETS startDate et endDate
    # ← Serveur retourne TOUTES les données ✅
)
```

---

## 📋 Scénarios Dates

### Scénario 1: User dit "analyse" (pas de dates)
```
User: "Analyse IBIS France"
→ OMETS startDate et endDate
→ Retour: TOUTES les données disponibles
```

### Scénario 2: User dit "30 derniers jours"
```
User: "Montre-moi les 30 derniers jours"
→ Calcule:
   endDate = aujourd'hui (ex: 2026-02-10)
   startDate = 30j avant (ex: 2026-01-11)
→ Passe: startDate="2026-01-11", endDate="2026-02-10"
```

### Scénario 3: User dit "décembre 2025"
```
User: "Données de décembre 2025"
→ Calcule:
   startDate = "2025-12-01"
   endDate = "2025-12-31"
→ Passe: startDate="2025-12-01", endDate="2025-12-31"
```

### Scénario 4: User dit "tout" ou "depuis le début"
```
User: "Montre tout", "Tous les données", "Depuis le début"
→ OMETS startDate et endDate
→ Retour: TOUTES les données ✅
```

### Scénario 5: User dit "7 derniers jours"
```
User: "Derniers 7 jours"
→ Calcule:
   endDate = aujourd'hui
   startDate = 7j avant
→ Passe les deux dates
```

---

## 🤖 Paramètre models

### ❌ MAUVAIS:
```python
User: "Analyse IBIS France"
↓
LLM: get_visibility_scores(
    domainId="...",
    topicId="...",
    models="GLOBAL"     # ← MAUVAIS! User n'a pas demandé un modèle
)
# Retour: Seulement GLOBAL, perd les autres modèles!
```

### ✅ BON:
```python
User: "Analyse IBIS France"
↓
LLM: get_visibility_scores(
    domainId="...",
    topicId="..."
    # ← OMETS models
    # ← Serveur retourne TOUS les modèles ✅
)
```

---

## 📊 Scénarios Models

### Modèles Disponibles (7 total)
```
1. GLOBAL                    ← Score combiné (défaut, meilleur pour vue générale)
2. gpt-5.1                   ← OpenAI GPT-5.1 (nouvelle)
3. sonar-pro                 ← Perplexity Sonar Pro
4. google-ai-overview        ← Google AI Overview
5. gpt-interface             ← GPT Interface (legacy)
6. gemini-3-pro-preview      ← Google Gemini 3 Pro Preview
7. gpt-5                     ← OpenAI GPT-5 (flagship)
```

### Scénario 1: User ne demande pas de modèle
```
User: "Analyse IBIS France"
→ OMETS le paramètre models
→ Retour: TOUS les modèles (GLOBAL + 6 autres) ✅
```

### Scénario 2: User demande un seul modèle
```
User: "Scores GPT-5.1 uniquement"
→ models="gpt-5.1"
→ Retour: Filtré sur GPT-5.1
```

### Scénario 3: User demande plusieurs modèles
```
User: "Compare GPT-5.1, Sonar Pro, et Gemini"
→ models="gpt-5.1,sonar-pro,gemini-3-pro-preview"
→ Format: Séparés par virgule, SANS espaces
→ Retour: Filtré sur ces 3 modèles + GLOBAL
```

### Scénario 4: User demande le GLOBAL
```
User: "Juste le score global"
→ models="GLOBAL"
→ Retour: GLOBAL uniquement (plus rapide)
```

### Scénario 5: User demande "tous"
```
User: "Tous les modèles", "Tous les scores"
→ OMETS le paramètre models
→ Retour: TOUS les modèles ✅
```

---

## 🔄 Combinaison: Dates + Models

### Cas 1: Aucun des deux
```
User: "Analyse IBIS France"
→ Omets startDate, endDate, models
→ Retour: TOUTES les données, TOUS les modèles
```

### Cas 2: Dates mais pas modèle
```
User: "Décembre 2025"
→ Passe: startDate="2025-12-01", endDate="2025-12-31"
→ Omets: models
→ Retour: Données décembre, TOUS les modèles
```

### Cas 3: Modèle mais pas dates
```
User: "GPT-5.1 uniquement"
→ Passe: models="gpt-5.1"
→ Omets: startDate, endDate
→ Retour: TOUTES les données, filtré sur GPT-5.1
```

### Cas 4: Dates ET modèles
```
User: "GPT-5.1 et Sonar pour décembre 2025"
→ Passe: 
   startDate="2025-12-01", 
   endDate="2025-12-31",
   models="gpt-5.1,sonar-pro"
→ Retour: Décembre 2025, 2 modèles
```

---

## 📊 Tableau Récapitulatif

| Cas | User Dit | startDate | endDate | models | Résultat |
|-----|----------|-----------|---------|--------|----------|
| 1 | "Analyse" | ❌ Omets | ❌ Omets | ❌ Omets | TOUT |
| 2 | "30 derniers jours" | ✅ Passe | ✅ Passe | ❌ Omets | 30j, tous modèles |
| 3 | "Décembre 2025" | ✅ Passe | ✅ Passe | ❌ Omets | Déc, tous modèles |
| 4 | "GPT-5.1" | ❌ Omets | ❌ Omets | ✅ Passe | Tout, GPT-5.1 |
| 5 | "GPT et Sonar" | ❌ Omets | ❌ Omets | ✅ Passe | Tout, 2 modèles |
| 6 | "Déc, GPT-5.1" | ✅ Passe | ✅ Passe | ✅ Passe | Déc, GPT-5.1 |
| 7 | "Tout" / "All" | ❌ Omets | ❌ Omets | ❌ Omets | TOUT |

---

## 🎯 Checklist: Est-ce que je dois passer le paramètre?

### Pour startDate/endDate:
```
☐ User mentionne "30 derniers jours"? → OUI, passe
☐ User mentionne "décembre"? → OUI, passe
☐ User mentionne "7 derniers jours"? → OUI, passe
☐ User mentionne "depuis 2025"? → OUI, passe
☐ User dit "analyse" (sans dates)? → NON, omets
☐ User dit "tout"? → NON, omets
☐ Pas sûr? → NON, omets (API retourne tout)
```

### Pour models:
```
☐ User demande "GPT-5.1"? → OUI, passe
☐ User demande "Sonar Pro"? → OUI, passe
☐ User demande "compare GPT et Gemini"? → OUI, passe: "gpt-5.1,gemini-3-pro-preview"
☐ User demande "GLOBAL"? → OUI, passe: "GLOBAL"
☐ User dit "analyse" (sans modèle)? → NON, omets
☐ User dit "tous les modèles"? → NON, omets
☐ Pas sûr? → NON, omets (API retourne tous)
```

---

## 💡 Exemples Concrets Complets

### Exemple 1: User = "Analyse IBIS France"
```python
# User ne mentionne RIEN
# → Omets tout

get_visibility_scores(
    domainId="694a6c9c454ba21fa497f50a",
    topicId="694a6d61454ba21fa4980103"
)

# Retour: TOUTES les données historiques, TOUS les modèles ✅
```

### Exemple 2: User = "30 derniers jours, juste GLOBAL"
```python
# User mentionne:
# - "30 derniers jours" → passe dates
# - "juste GLOBAL" → passe models

endDate = date.today().strftime("%Y-%m-%d")  # ex: 2026-02-10
startDate = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")  # ex: 2026-01-11

get_visibility_scores(
    domainId="694a6c9c454ba21fa497f50a",
    topicId="694a6d61454ba21fa4980103",
    startDate=startDate,  # "2026-01-11"
    endDate=endDate,      # "2026-02-10"
    models="GLOBAL"
)

# Retour: 30 derniers jours, GLOBAL uniquement
```

### Exemple 3: User = "Décembre 2025, compare GPT-5.1 vs Gemini"
```python
# User mentionne:
# - "Décembre 2025" → calcule dates
# - "GPT-5.1 vs Gemini" → passe models

get_visibility_scores(
    domainId="694a6c9c454ba21fa497f50a",
    topicId="694a6d61454ba21fa4980103",
    startDate="2025-12-01",
    endDate="2025-12-31",
    models="gpt-5.1,gemini-3-pro-preview"
)

# Retour: Décembre 2025, 2 modèles + GLOBAL
```

### Exemple 4: User = "Tous les données disponibles"
```python
# User demande TOUT

get_visibility_scores(
    domainId="694a6c9c454ba21fa497f50a",
    topicId="694a6d61454ba21fa4980103"
    # ← OMETS startDate, endDate, models
)

# Retour: TOUT depuis le début, TOUS les modèles ✅
```

### Exemple 5: User = "Janvier à mars 2026, tous les modèles"
```python
# User mentionne:
# - "Janvier à mars 2026" → dates
# - "tous les modèles" → OMETS models

get_visibility_scores(
    domainId="694a6c9c454ba21fa497f50a",
    topicId="694a6d61454ba21fa4980103",
    startDate="2026-01-01",
    endDate="2026-03-31"
    # ← OMETS models (user demande TOUS)
)

# Retour: Jan-Mar 2026, TOUS les modèles ✅
```

---

## 🚨 Erreurs Courantes

### ❌ Erreur 1: Toujours passer des dates par défaut
```python
# MAUVAIS:
get_visibility_scores(
    domainId=d,
    topicId=t,
    startDate="2026-01-01",     # Arbitraire!
    endDate="2026-02-10"        # User n'a rien demandé!
)
```

### ✅ Fix:
```python
# BON: User ne mentionne pas de dates → OMETS
get_visibility_scores(domainId=d, topicId=t)
```

---

### ❌ Erreur 2: Forcer GLOBAL par défaut
```python
# MAUVAIS:
get_visibility_scores(
    domainId=d,
    topicId=t,
    models="GLOBAL"     # User ne demande pas!
)
# Retour: Seulement GLOBAL, perd info multimodèle
```

### ✅ Fix:
```python
# BON: User ne demande pas de modèle → OMETS
get_visibility_scores(domainId=d, topicId=t)
# Retour: TOUS les modèles ✅
```

---

### ❌ Erreur 3: Espaces dans la liste models
```python
# MAUVAIS:
models="gpt-5.1, sonar-pro, gemini-3-pro-preview"  # Espaces!
# API ne reconnait pas
```

### ✅ Fix:
```python
# BON:
models="gpt-5.1,sonar-pro,gemini-3-pro-preview"  # Pas d'espaces
```

---

## 📞 Quick Reference

```
Utilisateur dit "analyse"
→ Omets startDate, endDate, models

Utilisateur dit "30 derniers jours"
→ Passe startDate (30j avant), endDate (aujourd'hui)
→ Omets models

Utilisateur dit "décembre 2025"
→ Passe startDate="2025-12-01", endDate="2025-12-31"
→ Omets models

Utilisateur dit "GPT-5.1"
→ Omets startDate, endDate
→ Passe models="gpt-5.1"

Utilisateur dit "GPT-5.1 pour décembre"
→ Passe startDate="2025-12-01", endDate="2025-12-31"
→ Passe models="gpt-5.1"

Utilisateur dit "tout"
→ Omets TOUT
```

---

## ✅ Résumé Final

```
┌─────────────────────────────────────────────────┐
│ RÈGLE D'OR:                                     │
│                                                 │
│ SI USER NE MENTIONNE PAS → OMETS LE PARAMÈTRE │
│ SI USER DEMANDE → PASSE LE PARAMÈTRE           │
│                                                 │
│ Quand en doute → OMETS (API retourne tout)   │
└─────────────────────────────────────────────────┘
```

---

**Voilà! Tu comprends maintenant les paramètres optionnels! 🎯**
