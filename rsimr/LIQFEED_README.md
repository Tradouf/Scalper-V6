# LIQFEED — flux de liquidations Hyperliquid (branché le 2026-08-08)

## Pourquoi

La fenêtre de tir RSI-MR (`FENETRE_DE_TIR_2026-08-08.md`) conditionne sur le
**régime de volatilité**, qui n'est qu'une *ombre* de la cause réelle : des
vendeurs contraints qui finissent par s'épuiser. Ce collecteur observe la
cause elle-même, pour répondre un jour à la seule question qui compte au
moment du signal : **le vendeur forcé a-t-il fini de vendre ?**

## Ce qu'Hyperliquid expose vraiment (sondé le 08-08, pas supposé)

| piste | résultat |
|---|---|
| `info` type `liquidations` / `allLiquidations` / … | ❌ 422, n'existe pas |
| souscription WS `liquidations` | ❌ rejetée par le serveur |
| hash nul dans le flux `trades` | ❌ faux marqueur (22 % des trades) |
| vault HLP dans les `users` des trades | ❌ 0 sur 888 trades |
| **`activeAssetCtx` (WS push)** | ✅ openInterest, funding, premium, mark |
| **`trades` (WS push)** | ✅ px, sz, côté taker, **les 2 contreparties** |
| **`userFills` (REST, public pour toute adresse)** | ✅ champ `liquidation` = {liquidatedUser, method}, `dir` = "Liquidated …" |
| sous-vaults HLP backstop | ✅ 2 adresses liquidatrices identifiées |

**Il n'existe donc pas de flux public de liquidations.** D'où une capture à
deux étages, qui est le meilleur substitut disponible.

## Architecture

**Étage 1 — signature (complète, push, coût API nul).**
Par coin et par seconde : `ΔopenInterest` + volume taker signé. Une
liquidation de longs fait **baisser** l'OI pendant que le taker vend ; un
short volontaire le fait **monter**. C'est exactement ce qui distingue le
vendeur contraint du vendeur qui choisit — et ça couvre les liquidations
exécutées contre le carnet, soit l'immense majorité.

**Étage 2 — vérité terrain (exacte, éparse).**
Quand une rafale est détectée (ΔOI ≤ −0,15 % sur 60 s **et** ≥ 60 %
d'agression vendeuse), on relève les contreparties présentes dans les trades
et on interroge leurs `userFills` : tout fill portant `liquidation` est
enregistré tel quel. Le sous-vault backstop HLP est sondé toutes les 10 min
(gratuit, mais rare : ~7 événements en 187 j).

**Garde-fou indispensable** : une vraie cascade fait éclater les 45 coins en
même temps (c'est sa définition) — soit ~270 requêtes d'un coup sur le
rate-limiter partagé avec les autres bots. Un budget global de 20 sondes/min
échantillonne la cascade au lieu d'étrangler l'infra.

**Piège évité** : sonder une adresse renvoie **tout son historique** (jusqu'à
2000 fills). Ces liquidations sont réelles et horodatées — on les garde,
c'est de l'historique gratuit — mais elles sont comptées à part, sinon une
rafale semblerait avoir provoqué des liquidations vieilles de six semaines.

## Exploitation

Base `rsimr/liq.db` (WAL) :
- `sec` : (ts_sec, coin) → oi, d_oi, mark, funding, premium, buy_ntl,
  sell_ntl, n_trades, max_ntl ;
- `liq` : liquidations confirmées (ts, coin, side, px, sz, ntl, dir,
  liquidated_user, method, source) — clé = tid, donc idempotent ;
- `probe` : journal des vérifications (rafale → combien de liquidations
  réellement trouvées) — **c'est lui qui mesurera si la signature de
  l'étage 1 attrape vraiment des liquidations.**

Service : `liqfeed.service` (user), log `logs/liqfeed.log`, univers = les
45 alts de la fenêtre de tir. Volume attendu : quelques Mo/jour.

## Ce que ça ne fait PAS

Collecte seule : **aucun ordre, aucune décision de trading**. Le paper
`rsimr/` n'est pas modifié et reste juge en aveugle. Aucune stratégie ne
s'appuiera sur ces données avant d'avoir (1) assez d'événements, (2) une
mesure de la fiabilité de la signature via `probe`, (3) un test confirmatoire
figé + gate placebo, comme pour tout le reste.
