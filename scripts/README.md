# Système d'audit autonome SalleDesMarches

Trois niveaux complémentaires de protection / auto-correction.

## Niveau 0 — Hard rules in-process (always-on)

Implémenté dans `main_v6.py` + `config/settings.py`. Aucune configuration
nécessaire, ces garde-fous s'exécutent à chaque tick du `_trail_loop` (2s).

| Mécanisme | Trigger | Action |
|---|---|---|
| Emergency hard exit | ROE ≤ -EMERGENCY_LOSS_ROE_MULT × SCALP_SL_PNL_PCT | Force close market reduce_only |
| SL fallback serré | Au recovery, si SL théorique passé | SL placé à current ± SL_FALLBACK_BUFFER_PCT |
| Flip override d'urgence | Position en perte > FLIP_EMERGENCY_LOSS_PCT + signal aligné régime | Seuil flip = MIN_CONFIDENCE au lieu de FLIP_MIN_CONFIDENCE |

## Niveau 2 — Audit Opus périodique

Script externe qui analyse les logs toutes les 6h via Claude Opus et ajuste
`config/settings.py` dans des bornes prédéfinies.

### Fichiers

- `scripts/audit_metrics.sh` — pré-agrégation bash des métriques (gratuit, déterministe)
- `scripts/audit_prompt.md` — prompt structuré (méthodologie, bornes, format)
- `scripts/audit.sh` — wrapper qui appelle `claude -p --model opus` headless
- `audit_log.md` — historique append-only des audits
- `audit_history/` — logs détaillés de chaque run

### Test manuel

```bash
cd /home/francois/SalleDesMarches_fixed
./scripts/audit.sh         # fenêtre 6h par défaut
./scripts/audit.sh 12      # fenêtre 12h
```

### Installation cron (toutes les 6h)

Ajouter au crontab utilisateur :

```bash
crontab -e
```

Puis ajouter :

```cron
0 */6 * * * cd /home/francois/SalleDesMarches_fixed && ./scripts/audit.sh >> audit_history/cron.log 2>&1
```

Les heures choisies (00:00, 06:00, 12:00, 18:00 UTC) couvrent un cycle complet
de marché. Pour une fréquence plus élevée :

```cron
0 */4 * * * ...    # toutes les 4h (6 audits/jour, ~$10-15/jour Opus)
```

### Garde-fous

- **Whitelist outils** : Claude n'a accès qu'à `Read`, `Edit`, `git add/commit/diff/log/status`. Pas de `Write`, pas de `Bash` arbitraire.
- **Scope fichier** : seul `config/settings.py`, `audit_log.md`, `code_proposals.md` peuvent être modifiés.
- **Bornes paramètres** : 12 paramètres autorisés, chacun avec min/max stricts (cf. `audit_prompt.md`).
- **Budget cap** : `--max-budget-usd 2.00` par run. Cron continue même si un audit échoue.
- **Anti-oscillation** : Claude lit `audit_log.md` avant de décider — il évite les changements répétés trop rapprochés.
- **Git commits** : chaque modif est commitée avec message `audit(opus): ...` → revert facile via `git revert`.

### Restart automatique

Après l'appel Opus, `audit.sh` détecte si le commit créé a touché `config/settings.py`.
Si oui, il appelle `./scripts/bot.sh restart` pour appliquer la nouvelle valeur.

Garde-fous :
1. Restart UNIQUEMENT si `config/settings.py` change (un commit qui ne touche que `audit_log.md` ou `code_proposals.md` ne déclenche rien).
2. **Anti-flap** : refus si un précédent restart a eu lieu il y a moins de 30 min. Empêche les boucles de redémarrage si plusieurs audits proposent des changements rapprochés. Marqueur stocké dans `audit_history/last_restart.ts`.
3. Skip si le bot n'est pas actif (utilisateur l'a stoppé exprès).

Pour revert un changement appliqué automatiquement :
```bash
git log --oneline | head -10           # trouver le commit audit
git revert <hash>
./scripts/bot.sh restart
```

### Suivi

Voir `audit_log.md` pour l'historique des décisions :

```bash
tail -50 audit_log.md
```

Les runs détaillés (sortie complète de Claude) sont dans `audit_history/run_*.log`.

## Niveau 1 — Self-tune loop in-process (futur)

Boucle Python interne au bot, ajustement instantané basé sur règles fixes.
Pas encore implémenté — le niveau 0 + niveau 2 couvrent les besoins critiques
pour l'instant.
