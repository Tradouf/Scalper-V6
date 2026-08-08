# GrokWatch — tracker paper des signaux Grok par email

Enregistre chaque signal BTC reçu par email (« BTC Quid is ready ») avec le
prix Hyperliquid au moment de la réception, puis mesure a posteriori le
rendement net de frais à +1 h / +4 h / +24 h. **Aucun ordre n'est passé** :
c'est un instrument de mesure, pas un bot. On ne branche une exécution que si
l'agrégat montre une espérance nette positive sur un échantillon décent
(≥ 20–30 signaux).

## Usage

```bash
# Ingestion manuelle (mail collé dans un fichier ou sur stdin)
python -m grokwatch.ingest email.txt
cat email.txt | python -m grokwatch.ingest

# Poller IMAP permanent (config .env, voir ci-dessous)
python -m grokwatch.poller           # boucle (300 s par défaut)
python -m grokwatch.poller --once    # un passage (debug ou cron)

# Verdict
python -m grokwatch.evaluate

# Tests
python -m pytest tests/test_grokwatch.py -v
```

## Config IMAP (.env)

```bash
GROKWATCH_IMAP_HOST=imap.gmail.com     # ou 127.0.0.1 pour Proton Bridge
GROKWATCH_IMAP_PORT=993                # 1143 + STARTTLS pour Proton Bridge
GROKWATCH_IMAP_USER=...
GROKWATCH_IMAP_PASSWORD=...            # mot de passe d'application
GROKWATCH_IMAP_FOLDER=INBOX
GROKWATCH_SENDER=                      # filtre expéditeur (sous-chaîne, optionnel)
GROKWATCH_SUBJECT=quid is ready        # filtre sujet (sous-chaîne)
GROKWATCH_POLL_SEC=300
```

Boîte Proton : pas d'IMAP direct. Deux options —
1. **Transfert automatique** vers une adresse Gmail dédiée (le plus simple) :
   règle Proton → forward, puis mot de passe d'application Gmail ici.
2. **Proton Mail Bridge** (plan payant) : host 127.0.0.1, port 1143,
   identifiants fournis par Bridge.

## Fichiers d'état (`grokwatch/state/`)

- `signals.jsonl` — un signal par ligne (direction, symbole, mid à réception,
  levier/taille suggérés, hash de contenu pour dédoublonnage)
- `poller.json` — dernier UID IMAP traité (jamais de retraitement)

## Notes

- Prix à réception : mid `allMids` via le rate-limiter HL partagé
  (`hl_rate_limit`) ; l'évaluation passe par `simplebot.data.fetch_ohlcv`
  (cache disque anti-429 partagé).
- Frais par défaut : aller-retour taker 0.09 % (`GROKWATCH_FEE_PCT` pour
  ajuster).
- Le parser reconnaît « Position recommandée : SHORT/LONG XXX-PERP »
  (FR/EN, HTML toléré). Si Grok change de format, adapter
  `grokwatch/parser.py` et ses tests.
