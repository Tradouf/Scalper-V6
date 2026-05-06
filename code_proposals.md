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
**Status** : applied 2026-05-06 (commit à venir) — `_llm` retry/None déjà en place, ajouté `llm_status="down"` dans agents (technical, momentum, risk_entry), `_consensus` retourne `side="llm_down"` distinct de `wait`, log `LLM_DEGRADED` en warning au lieu de SKIP conf.

---

## 2026-05-06 07:05 — [WARNING] Doublons de processus bot — pas de PID file lock

**Severity** : warning
**Files** : `main_v6.py:2489-2512` (fonction `main`)
**Pattern** : Au cours des sessions du 2026-05-04 et 2026-05-05, plusieurs occurrences de **deux processus `python3 main_v6.py` tournant en parallèle** ont été observées (PIDs 22470/111971, 129854/134390, 185507/190063, 191619/195189, 228671/228847). Causes constatées :
- `nohup ... &` lancé deux fois (manuel + cron, ou shell imbriqué qui dédouble)
- `start_sdm.sh` invoqué pendant qu'un bot tourne déjà
- Crash silencieux d'un parent qui laisse le child orphelin actif

Conséquences observées :
1. **Tous les logs dupliqués** — chaque ligne apparaît 2× (chaque process a son propre handler sur `logs/sdm.log`).
2. **Métriques d'audit faussées** (compteurs ×2 — corrigé partiellement par la dédup `awk` dans `audit_metrics.sh`).
3. **Race conditions sur la gestion des positions** — les deux bots placent et annulent des SL/TP en parallèle, créant des orphelins (cf. incident BNB orphan SL du 2026-05-05).
4. **Double LLM load** sur LocalAI (déjà saturé), 2× plus de timeouts, donc 2× plus de `tech=0.00`.

**Diagnostic** : Le bot ne vérifie pas qu'aucune autre instance ne tourne avant de démarrer. Aucun PID file, aucun lock fichier, aucun port unique. La fonction `main()` (l. 2489) crée directement le bot et tombe dans `run_forever()` sans contrôle d'unicité.

**Proposed fix** :
```python
# Before — main_v6.py
def main() -> None:
    symbols = getattr(SETTINGS, "SYMBOLS", ["BTC", "ETH", "ATOM", "DYDX", "SOL"])
    bot = SalleDesMarchesV6(symbols=symbols, simulation=SIMULATION_MODE)
    ...

# After — PID lock + cleanup
import fcntl, os, atexit

PID_FILE = "logs/sdm.pid"

def _acquire_singleton_lock() -> int:
    """Ouvre un fichier PID, prend un lock exclusif non-bloquant, écrit le PID.
    Échoue avec sys.exit(1) si une autre instance tient déjà le lock.
    Le lock est libéré automatiquement à la mort du process (kernel)."""
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    fd = os.open(PID_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        with open(PID_FILE) as f:
            existing = f.read().strip()
        logger.error("Une autre instance tourne déjà (PID=%s). Abandon.", existing)
        sys.exit(1)
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    atexit.register(lambda: os.unlink(PID_FILE) if os.path.exists(PID_FILE) else None)
    return fd  # garder le fd vivant pour ne pas perdre le lock

def main() -> None:
    _lock_fd = _acquire_singleton_lock()  # noqa: F841 — gardé en scope
    symbols = getattr(SETTINGS, "SYMBOLS", ["BTC", "ETH", "ATOM", "DYDX", "SOL"])
    bot = SalleDesMarchesV6(symbols=symbols, simulation=SIMULATION_MODE)
    ...
```

**Risk si non corrigé** : Race conditions répétées qui produisent des orphelins, des cancellations parasites, et des positions sans SL. Les deux incidents critiques de cette session (BNB orphan SL 2026-05-05, BTC -5.88% du 2026-05-04) avaient en partie cette cause. Sans correction, chaque restart maladroit recrée le risque.
**Status** : applied 2026-05-06 (commit à venir) — `_acquire_singleton_lock()` ajouté, fcntl.flock sur logs/sdm.pid, sortie sys.exit(1) si lock déjà tenu, atexit cleanup.

---
