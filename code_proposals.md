# Propositions de correction de code

Append-only. Écrit par les audits Opus quand un bug structurel ou une amélioration
non-paramétrique est identifié. **Aucune ne s'applique automatiquement** — chaque
proposition doit être revue, ajustée et appliquée manuellement.

## Format

Chaque proposition contient :
- **Sévérité** : `critical` (bug capital) / `warning` (dégradation) / `info` (amélioration)
- **Files** : fichiers à modifier avec lignes
- **Pattern** : évidence dans les logs / métriques
- **Diagnostic** : pourquoi c'est un problème
- **Proposed fix** : code before/after
- **Risk** : impact si on ne fait rien
- **Status** : `pending` / `applied` / `rejected` / `superseded`

Pour appliquer une proposition :
1. Relire le contexte complet (logs cités, fichiers cités).
2. Adapter le diff au code actuel (Opus peut s'être basé sur une version périmée).
3. Tester en simulation si possible.
4. Mettre `Status: applied` avec la date et le commit hash.

Pour rejeter :
1. Mettre `Status: rejected` avec la raison.
2. Ainsi Opus saura qu'il est inutile de re-proposer la même chose.

---

## 2026-05-05 13:00 — [INFO] LLM timeouts → conf=0.00 → ENTER=0 sur fenêtres entières

**Severity** : info
**Files** : `agents/base_agent.py` (méthode `_llm`), `agents/agent_technical.py` (consommateur le plus impacté). À cibler aussi : `main_v6.py` côté `compute_consensus` pour la dégradation propre.
**Pattern** : 30 erreurs `LLM error: HTTPConnectionPool(host='localhost', port=8080): Read timed out. (read timeout=90)` sur 6h (~5/h). Logs corrélés type `CONSENSUS APE | side=wait conf=0.00 | bull=0.70 tech=0.00 bear_risk=low` puis `SKIP — conf trop faible (0.00 < 0.65) ou côté=wait`. ENTER=0 sur 2 audits consécutifs (12h cumulées).
**Diagnostic** : Quand l'appel LLM technique time out à 90s, l'agent renvoie une analyse vide (tech=0.00). Le calcul de consensus tombe alors à conf=0.00 et side=wait, et le filtre MIN_CONFIDENCE=0.65 rejette l'entrée. Sur la fenêtre observée, aucune entrée n'est ouverte malgré 124 calculs de consensus. Le bot devient passif durant les pics de saturation LocalAI, sans que cela soit traçable comme erreur métier (le SKIP conf=0.00 ressemble à un signal honnêtement neutre). Aucun ajustement de paramètre ne corrige ce cas : 0.00 reste sous tout seuil ≥ 0.55 (borne min de MIN_CONFIDENCE).
**Proposed fix** :
```python
# Before — agents/base_agent.py (illustratif, à valider sur le code réel)
def _llm(self, prompt, model=None, **kwargs):
    resp = requests.post(f"{LOCALAI_BASE_URL}/chat/completions",
                         json=payload, timeout=90)
    return resp.json()

# After — 1 retry court + flag d'échec explicite, le caller décide quoi faire
def _llm(self, prompt, model=None, **kwargs):
    last_err = None
    for attempt in (1, 2):
        try:
            resp = requests.post(f"{LOCALAI_BASE_URL}/chat/completions",
                                 json=payload, timeout=60)
            return resp.json()
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = e
            continue
    self.logger.warning(f"LLM unavailable after retry: {last_err}")
    return None  # caller doit interpréter None ≠ "signal neutre"
```
Et côté `compute_consensus` (main_v6.py) : si `tech is None` (LLM down) plutôt que `tech=0.00` (signal neutre), produire `side="skip_llm_down"` distinct de `wait`, et logger `LLM_DEGRADED` au lieu de `SKIP conf trop faible`. Permet de mesurer la perte d'opportunité réelle.
**Risk si non corrigé** : Pendant les pics de charge LocalAI, le bot reste flat indéfiniment. Sur 12h observées, 0 entrée ouverte alors que 124 cycles de consensus ont eu lieu. La cause est invisible dans les métriques actuelles (ressemble à un marché sans signal), ce qui retarde le diagnostic à chaque cycle.
**Status** : pending

---
