"""
RSIMRLiveTrader — exécuteur live du rachat de survente RSI, wallet HL2.

⚠️ DRY-RUN PAR DÉFAUT. Le live réel exige RSIMR_DRY_RUN=0 explicitement.
   Cet exécuteur n'a JAMAIS passé d'ordre réel à ce jour.

POURQUOI CE FICHIER EXISTE (et pourquoi il n'est pas armé)
----------------------------------------------------------
`paper.py` ne détient aucun client d'exchange : passer en live ne consiste pas
à basculer un drapeau, il fallait écrire ce chemin d'ordre. L'infra de sécurité
est reprise telle quelle de SimpleBot → xsmom (kill-switch à hystérésis,
fail-safe lecture de compte, exécution maker-first, verrou, écriture atomique)
car c'est la seule partie du projet dont le live a validé la fiabilité.

Le candidat RSI-MR est confirmé en BACKTEST (OOS +26.7 bps, placebo p=0.024)
mais son paper a démarré le 07-08 22:24 : le verdict est prévu mi-septembre.
Rien ici ne remplace ce verdict.

DIFFÉRENCES ASSUMÉES AVEC LE PAPER
----------------------------------
Le paper prend TOUS les signaux à notionnel fixe. Ce live applique en plus la
fenêtre de tir (`FENETRE_DE_TIR_2026-08-08.md`), car ignorer une mesure aussi
nette serait dépenser des frais sciemment :

  - régime CALME à l'entrée → AUCUN trade (edge brut mesuré −0.5 bps, donc
    rien à capter ; net −15.5 bps in-sample, −26.7 en OOS) ;
  - régime NORMAL → taille pleine ; régime TEMPÊTE → 0.55× (même espérance,
    4× la variance).

Conséquence à assumer : le live ne reproduit donc PAS la statistique du paper
(il en trade un sous-ensemble). C'est voulu, et c'est documenté ici pour que
la comparaison ultérieure ne soit pas faussée.

SÉCURITÉS
---------
  - wallet : HL2 (celui de SimpleBot, arrêté et disabled le 07-08). Refuse de
    démarrer si la clé est identique à celle d'un bot ACTIF (V7, llmbot, xsmom) ;
  - kill-switch : perte ≥ RSIMR_KILL_LOSS_PCT du pic sur 24 h, confirmée sur
    N lectures, → fermeture de tout et pause ; gel après N échecs de lecture ;
  - plafond d'exposition : notionnel total ≤ RSIMR_MAX_GROSS_PCT × equity ;
  - sortie temporelle stricte : toute position ouverte depuis > H_BARS heures
    est fermée, y compris après un redémarrage (l'état est persisté) ;
  - min notionnel HL respecté, sinon le signal est sauté et compté.

JOURNAL DES TRADES
------------------
Chaque clôture est écrite dans `state/live_trades.jsonl` (une ligne JSON par
trade, horodatée). L'état ne portait qu'un cumul `realized_usd` depuis
toujours : impossible d'en tirer un résultat sur 24 h ou 7 jours. Le journal
sert exactement à ça, et il porte le drapeau `dry_run` de chaque trade pour
qu'un passage en dry-run ne vienne jamais polluer les chiffres du réel.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from rsimr.paper import (FEE_SIDE, H_BARS, HOUR_MS, MIN_BARS, RSI_N, SYMBOLS,
                         rsi_series)
from simplebot.data import closed_candles, fetch_ohlcv
from simplebot.execution import smart_close, smart_entry

logger = logging.getLogger("sdm.rsimr.live")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ── Config (env RSIMR_*) ─────────────────────────────────────────────────────
DRY_RUN = os.environ.get("RSIMR_DRY_RUN", "1") not in ("0", "false", "False")

ENV_PRIVATE_KEY = "HL2_PRIVATE_KEY"
ENV_ACCOUNT_ADDRESS = "HL2_ACCOUNT_ADDRESS"
# wallets de bots ACTIFS : partager la clé avec eux est interdit.
_FORBIDDEN_KEY_ENVS = ("HL_PRIVATE_KEY", "HL3_PRIVATE_KEY", "HL4_PRIVATE_KEY")

MIN_NOTIONAL_USD = _env_float("RSIMR_MIN_NOTIONAL", 11.0)   # min HL 10 $ + arrondi
NOTIONAL_PCT = _env_float("RSIMR_NOTIONAL_PCT", 0.12)       # part d'equity/trade
MAX_CONCURRENT = _env_int("RSIMR_MAX_CONCURRENT", 8)
MAX_GROSS_PCT = _env_float("RSIMR_MAX_GROSS_PCT", 1.0)      # notionnel/equity
FETCH_THROTTLE_SEC = _env_float("RSIMR_FETCH_THROTTLE_SEC", 0.35)

KILL_LOSS_PCT = _env_float("RSIMR_KILL_LOSS_PCT", 0.05)
KILL_WINDOW_SEC = _env_int("RSIMR_KILL_WINDOW_SEC", 24 * 3600)
KILL_PAUSE_SEC = _env_int("RSIMR_KILL_PAUSE_SEC", 24 * 3600)
KILL_CONFIRMATIONS = _env_int("RSIMR_KILL_CONFIRMATIONS", 2)
KILL_MAX_READ_FAILURES = _env_int("RSIMR_KILL_MAX_READ_FAILURES", 3)

DRY_EQUITY0 = _env_float("RSIMR_DRY_EQUITY", 207.0)
FETCH_DAYS = _env_float("RSIMR_FETCH_DAYS", 11.0)

# Fenêtre de tir (FENETRE_DE_TIR_2026-08-08.md) — désactivable pour comparer
# au paper, mais activée par défaut : le régime calme n'a pas d'edge brut.
REGIME_FILTER = os.environ.get("RSIMR_REGIME_FILTER", "1") not in ("0", "false", "False")
# HMM canonique K=3 de l'étape 2 (états triés par volatilité croissante)
A_CAN = np.array([[0.905, 0.086, 0.009],
                  [0.050, 0.904, 0.045],
                  [0.002, 0.204, 0.794]])
VOL_RATIOS = np.array([41.0, 82.2, 212.5]) / 100.0
STATIONARY = np.array([0.30, 0.56, 0.14])
# sizing dérivé des E/Var MESURÉS par régime (0 = ne pas trader)
REGIME_SIZE = {0: 0.0, 1: 1.00, 2: 0.55}

STATE_FILE = Path(os.environ.get(
    "RSIMR_LIVE_STATE_FILE",
    str(Path(__file__).resolve().parent / "state" / "rsimr_live_state.json"),
))
# Journal des clôtures, à côté de l'état (le paper a le sien, trades.jsonl).
TRADES_NAME = "live_trades.jsonl"


def make_live_client():
    """Client HL sur le wallet HL2. Refuse une clé partagée avec un bot actif.

    `HL2_PRIVATE_KEY` est une API wallet (agent) : elle SIGNE pour le compte
    maître `HL2_ACCOUNT_ADDRESS`, mais ne détient rien elle-même. Sans passer
    l'adresse du maître, toutes les lectures portent sur l'agent et l'equity
    vaut 0 — le bot ne verrait aucun capital et ne trierait rien, en silence.
    L'adresse maître est donc OBLIGATOIRE ici.
    """
    key = (os.environ.get(ENV_PRIVATE_KEY) or "").strip()
    if not key:
        raise RuntimeError(
            f"{ENV_PRIVATE_KEY} manquant — le live RSI-MR exige le wallet HL2")
    for other in _FORBIDDEN_KEY_ENVS:
        if key and key == (os.environ.get(other) or "").strip():
            raise RuntimeError(
                f"{ENV_PRIVATE_KEY} identique à {other} — un bot ACTIF utilise "
                "ce wallet, partage interdit")
    master = (os.environ.get(ENV_ACCOUNT_ADDRESS) or "").strip()
    if not master:
        raise RuntimeError(
            f"{ENV_ACCOUNT_ADDRESS} manquant — sans le compte maître, l'equity "
            "lue serait celle de l'API wallet (0 $) et aucun ordre ne partirait")
    from hyperliquid_client import HyperliquidClient
    return HyperliquidClient(wallet_key=key, account_address=master)


def filtered_regime(closes: List[float]) -> int:
    """État de volatilité filtré au dernier close — causal, aucun lookahead.

    Le régime est RELATIF à la volatilité propre du symbole sur la fenêtre
    fournie (les rendements sont divisés par leur écart-type avant filtrage),
    exactement comme dans l'analyse de la fenêtre de tir : le modèle canonique
    a été calibré sur des rendements standardisés par symbole. Un actif
    structurellement calme n'est donc pas classé « calme » en permanence —
    c'est bien le calme RELATIF, celui où l'edge disparaît, qui est détecté.
    """
    if len(closes) < 50:
        return 1
    c = np.asarray(closes, dtype=float)
    r = np.diff(np.log(c))
    sigma = float(r.std())
    if not np.isfinite(sigma) or sigma <= 0:
        return 1
    x = r / sigma
    var = VOL_RATIOS ** 2
    logA = np.log(A_CAN)
    la = np.log(STATIONARY) - 0.5 * (np.log(2 * np.pi * var) + x[0] ** 2 / var)
    for t in range(1, len(x)):
        m = np.max(la[:, None] + logA, axis=0)
        la = (-0.5 * (np.log(2 * np.pi * var) + x[t] ** 2 / var) + m
              + np.log(np.sum(np.exp(la[:, None] + logA - m[None, :]), axis=0)))
        la -= la.max()
    return int(np.argmax(la))


class RSIMRLiveTrader:
    """
    Un sweep par heure UTC (cadence du paper) : ferme ce qui a atteint H_BARS,
    ouvre les nouveaux signaux admis par la fenêtre de tir.

    En dry-run : aucun appel d'ordre, fills simulés au close avec frais maker,
    mais tout le chemin décision → sizing → exécution → comptabilité est celui
    du live.
    """

    def __init__(self, client=None, dry_run: Optional[bool] = None,
                 fetch: Optional[Callable[..., List[dict]]] = None,
                 symbols: Optional[List[str]] = None):
        self.dry_run = DRY_RUN if dry_run is None else dry_run
        self.symbols = list(symbols) if symbols else list(SYMBOLS)
        self._fetch = fetch or fetch_ohlcv
        self.frozen_reason: Optional[str] = None
        self._acct_read_failures = 0
        self._kill_breach_count = 0
        # dérivé de STATE_FILE : un état isolé (tests, instance parallèle)
        # emporte son journal avec lui, sans variable d'env supplémentaire.
        self.trades_file = STATE_FILE.parent / TRADES_NAME
        if client is not None:
            self.client = client
        elif self.dry_run:
            self.client = None
        else:
            self.client = make_live_client()
        self.state = self._load_state()
        # tracé dans l'état pour que le dashboard ne devine jamais le mode
        self.state["dry_run"] = self.dry_run
        if not self.dry_run:
            # Garde « equity fantôme » : une equity nulle ne signifie pas
            # « pas d'argent », elle signifie presque toujours qu'on lit la
            # mauvaise adresse (API wallet au lieu du compte maître). Dans ce
            # cas le bot ne trade rien tout en paraissant sain — échec bruyant
            # exigé, jamais un silence.
            equity = self._account_value()
            if equity <= 0:
                raise RuntimeError(
                    f"equity live lue à {equity:.2f} $ — lecture sur la mauvaise "
                    f"adresse ou wallet vide ; refus de démarrer")
            logger.info("equity live confirmée : %.2f $", equity)
        # persisté tout de suite : sans cela le mode n'apparaîtrait qu'au
        # premier sweep horaire, et le dashboard afficherait « dry-run » sur un
        # bot armé — l'ambiguïté à ne jamais laisser exister.
        self._save_state()

    # ── État ────────────────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        st: dict = {}
        if STATE_FILE.exists():
            try:
                st = json.loads(STATE_FILE.read_text())
            except Exception as e:
                logger.error("état illisible (%r) — repart à vide", e)
                st = {}
        st.setdefault("positions", {})       # sym -> {dir, entry, sz, notional, opened_ms, regime}
        st.setdefault("dry_equity", DRY_EQUITY0)
        st.setdefault("realized_usd", 0.0)
        st.setdefault("equity_peak", 0.0)
        st.setdefault("peak_ts", time.time())
        st.setdefault("paused_until", 0.0)
        st.setdefault("last_sweep_hour", 0)
        st.setdefault("n_trades", 0)
        st.setdefault("skipped", {"min_notional": 0, "regime_calm": 0,
                                  "slots": 0, "gross_cap": 0})
        st.setdefault("exec_stats", {"maker": 0, "taker": 0, "mixed": 0, "skip": 0})
        return st

    def _save_state(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        os.replace(tmp, STATE_FILE)

    def _log_trade(self, rec: dict) -> None:
        """Une ligne JSON par clôture. Jamais bloquant : un journal
        inaccessible ne doit pas empêcher le bot de trader."""
        try:
            self.trades_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.trades_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception as e:
            logger.warning("Écriture %s échouée: %r", TRADES_NAME, e)

    def _record_close(self, sym: str, pos: dict, pnl: float, exit_px: float,
                      reason: str, now: float,
                      px_estimated: bool = False,
                      keep_position: bool = False) -> None:
        """Point de passage UNIQUE d'une clôture : retire la position, met à
        jour la comptabilité, journalise. Centralisé pour que la sortie
        d'urgence ne puisse plus échapper au compteur — c'était le cas, et
        c'est précisément la sortie qu'on ne veut pas perdre de vue.

        keep_position=True : clôture PARTIELLE — le PnL des lots fermés est
        comptabilisé et journalisé, mais le reliquat (déjà redimensionné par
        l'appelant) reste dans l'état pour être refermé au sweep suivant."""
        if not keep_position:
            self.state["positions"].pop(sym, None)
        self.state["realized_usd"] += pnl
        self.state["n_trades"] += 1
        if self.dry_run:
            self.state["dry_equity"] += pnl
        notional = float(pos.get("notional") or 0.0)
        opened_ms = int(pos.get("opened_ms") or 0)
        self._log_trade({
            "closed_at": int(now),
            "sym": sym,
            "dir": pos.get("dir"),
            "entry_px": pos.get("entry"),
            "exit_px": exit_px,
            "sz": pos.get("sz"),
            "notional": notional,
            "pnl_usd": round(pnl, 6),
            "net_bps": round(pnl / notional * 10_000, 2) if notional else None,
            "regime": pos.get("regime"),
            "opened_ms": opened_ms,
            "held_h": round((now * 1000 - opened_ms) / 3_600_000, 2)
                      if opened_ms else None,
            "reason": reason,
            "px_estimated": px_estimated,
            "dry_run": self.dry_run,
        })

    # ── Compte ──────────────────────────────────────────────────────────────

    def _account_value(self) -> float:
        """Valeur canonique — sizing ET kill-switch. Peut lever (fail-safe)."""
        if self.dry_run:
            return float(self.state["dry_equity"])
        return float(self.client.get_portfolio_value())

    def _emergency_flatten(self, ref: Dict[str, float],
                           now: Optional[float] = None) -> None:
        now = now or time.time()
        for sym in sorted(list(self.state["positions"])):
            pos = self.state["positions"][sym]
            if self.dry_run:
                px = ref.get(sym, pos["entry"])
                self._record_close(sym, pos, self._pnl(pos, px, FEE_SIDE), px,
                                   "kill_switch", now)
                continue
            try:
                self.client.cancel_all_orders(sym)
            except Exception as e:
                logger.warning("%s: cancel_all_orders: %r", sym, e)
            try:
                res = self.client.market_close(sym) or {}
                # Une fermeture au marché est taker par construction. Si la
                # réponse ne porte pas de prix, on retombe sur la référence en
                # marquant le PnL comme estimé plutôt que de le perdre.
                px = float(res.get("avg_px") or 0.0)
                estimated = px <= 0
                if estimated:
                    px = ref.get(sym) or pos["entry"]
                    logger.warning("%s: prix de sortie absent — PnL estimé", sym)
                pnl = self._pnl(pos, px, 0.00075)
                self._record_close(sym, pos, pnl, px, "kill_switch", now,
                                   px_estimated=estimated)
                logger.info("%s: position fermée (kill-switch) — PnL %+.3f$",
                            sym, pnl)
            except Exception as e:
                logger.critical("%s: FERMETURE D'URGENCE ÉCHOUÉE: %r", sym, e)

    def kill_switch_engaged(self, now: Optional[float] = None,
                            ref: Optional[Dict[str, float]] = None) -> bool:
        now = now or time.time()
        if self.frozen_reason:
            return True
        if now < self.state.get("paused_until", 0.0):
            return True
        try:
            equity = self._account_value()
            self._acct_read_failures = 0
        except Exception as e:
            self._acct_read_failures += 1
            logger.error("lecture equity échouée (%d/%d): %r",
                         self._acct_read_failures, KILL_MAX_READ_FAILURES, e)
            if self._acct_read_failures >= KILL_MAX_READ_FAILURES:
                self.frozen_reason = "equity illisible"
                logger.critical("GEL: equity illisible %d fois de suite",
                                self._acct_read_failures)
            return True
        peak = float(self.state.get("equity_peak") or 0.0)
        if now - float(self.state.get("peak_ts") or 0.0) > KILL_WINDOW_SEC:
            peak = 0.0
        if equity > peak:
            self.state["equity_peak"] = equity
            self.state["peak_ts"] = now
            self._kill_breach_count = 0
            return False
        if peak > 0 and equity <= peak * (1.0 - KILL_LOSS_PCT):
            self._kill_breach_count += 1
            if self._kill_breach_count < KILL_CONFIRMATIONS:
                logger.warning("kill-switch: %.2f ≤ pic %.2f ×(1-%.1f%%) "
                               "— confirmation %d/%d", equity, peak,
                               100 * KILL_LOSS_PCT, self._kill_breach_count,
                               KILL_CONFIRMATIONS)
                return True
            logger.critical("KILL-SWITCH: %.2f ≤ pic %.2f — fermeture + pause %dh",
                            equity, peak, KILL_PAUSE_SEC // 3600)
            self._emergency_flatten(ref or {}, now)
            self.state["paused_until"] = now + KILL_PAUSE_SEC
            self._save_state()
            return True
        self._kill_breach_count = 0
        return False

    # ── Exécution ───────────────────────────────────────────────────────────

    @staticmethod
    def _pnl(pos: dict, fill_px: float, fee_pct: float) -> float:
        pnl_pct = pos["dir"] * (fill_px - pos["entry"]) / pos["entry"] - fee_pct
        return pos["notional"] * pnl_pct

    def _exec_close(self, sym: str, pos: dict,
                    ref_px: float) -> Optional[Tuple[float, float, float]]:
        """Ferme ; renvoie (PnL des lots fermés, prix de sortie, taille
        réellement fermée), ou None si rien n'a été exécuté. Le PnL est
        calculé sur la taille FERMÉE, pas sur la taille demandée — sur une
        fermeture partielle, les deux diffèrent (bug LTC du 15-08 : ordre
        envoyé à 0.56 pour une position de 0.57, reliquat 0.01 orphelin sur
        l'exchange). C'est l'appelant qui décide du sort du reliquat."""
        if self.dry_run:
            self.state["exec_stats"]["maker"] += 1
            return self._pnl(pos, ref_px, FEE_SIDE), ref_px, pos["sz"]
        res = smart_close(self.client, sym, is_buy=(pos["dir"] == -1),
                          sz=pos["sz"], ref_price=ref_px)
        mode = res.get("mode", "skip")
        self.state["exec_stats"][mode] = self.state["exec_stats"].get(mode, 0) + 1
        filled = min(float(res.get("total_sz") or 0.0), pos["sz"])
        if mode == "skip" or filled <= 0:
            logger.warning("%s: sortie non exécutée — position conservée", sym)
            return None
        fee = FEE_SIDE if mode == "maker" else 0.00075
        frac = filled / pos["sz"]
        part = dict(pos, sz=filled, notional=pos["notional"] * frac)
        return self._pnl(part, res["avg_px"], fee), float(res["avg_px"]), filled

    def _exec_open(self, sym: str, notional: float, ref_px: float,
                   regime: int, now_ms: int) -> Optional[dict]:
        if notional < MIN_NOTIONAL_USD:
            self.state["skipped"]["min_notional"] += 1
            logger.warning("%s: notionnel %.2f$ < min %.0f$ — signal sauté",
                           sym, notional, MIN_NOTIONAL_USD)
            return None
        sz = notional / ref_px
        if self.dry_run:
            fill_px = ref_px
            self.state["exec_stats"]["maker"] += 1
        else:
            res = smart_entry(self.client, sym, is_buy=True, sz=sz,
                              ref_price=ref_px)
            mode = res.get("mode", "skip")
            self.state["exec_stats"][mode] = (
                self.state["exec_stats"].get(mode, 0) + 1)
            if mode == "skip" or res.get("total_sz", 0) <= 0:
                return None
            fill_px, sz = res["avg_px"], res["total_sz"]
        return {"dir": 1, "entry": fill_px, "sz": sz, "notional": sz * fill_px,
                "opened_ms": now_ms, "regime": regime}

    # ── Sweep horaire ───────────────────────────────────────────────────────

    def sweep_if_due(self, now: Optional[float] = None) -> bool:
        now = now or time.time()
        hour = int(now // 3600)
        if self.state.get("last_sweep_hour") == hour:
            return False

        now_ms = int(now * 1000)
        data: Dict[str, List[dict]] = {}
        for sym in self.symbols:
            try:
                candles = closed_candles(
                    self._fetch(sym, "1h", FETCH_DAYS), HOUR_MS, now_ms)
            except Exception as e:
                logger.warning("%s: fetch échoué: %r", sym, e)
                continue
            if len(candles) >= MIN_BARS:
                data[sym] = candles
            time.sleep(FETCH_THROTTLE_SEC)
        if not data:
            logger.error("aucune donnée ce sweep — rien fait")
            return False

        ref = {s: c[-1]["close"] for s, c in data.items()}
        if self.kill_switch_engaged(now, ref):
            self.state["last_sweep_hour"] = hour
            self._save_state()
            return False

        # 1. sorties dues — avant les entrées. Comparaison en INDEX D'HEURE,
        # pas en secondes écoulées : les sweeps dérivent de quelques dizaines
        # de secondes (entrée 02:03:28, sweep 06:02:46 → 3 h 59 min 18 s) et
        # un chrono strict de 4 h ratait la sortie d'une heure entière
        # (observé sur LTC le 15-08 : tenue 5 h au lieu de 4). L'index d'heure
        # reproduit la règle du backtest : sortir au close de la 4e bougie.
        for sym in sorted(list(self.state["positions"])):
            pos = self.state["positions"][sym]
            if now_ms // HOUR_MS - int(pos["opened_ms"]) // HOUR_MS < H_BARS:
                continue
            px = ref.get(sym)
            if px is None:
                logger.warning("%s: pas de prix — sortie reportée", sym)
                continue
            closed = self._exec_close(sym, pos, px)
            if closed is None:
                continue
            pnl, exit_px, filled = closed
            residual = pos["sz"] - filled
            fully = residual <= 1e-9 * max(1.0, pos["sz"])
            if fully:
                self._record_close(sym, pos, pnl, exit_px, "timed_exit", now)
            else:
                # journaliser la part fermée avec SA taille, garder le reliquat
                frac = filled / pos["sz"]
                part = dict(pos, sz=filled, notional=pos["notional"] * frac)
                pos["sz"] = residual
                pos["notional"] = pos["notional"] * (1.0 - frac)
                self._record_close(sym, part, pnl, exit_px, "partial_exit",
                                   now, keep_position=True)
                logger.warning("%s: fermeture PARTIELLE %.6f — reliquat %.6f "
                               "conservé, nouvelle tentative au prochain sweep",
                               sym, filled, residual)
            logger.info("SORTIE %s après %dh — PnL %+.3f$ (régime %s)%s",
                        sym, H_BARS, pnl, pos.get("regime"),
                        "" if fully else " [PARTIELLE]")

        # 2. entrées : RSI passe de ≤30 à >30 sur la dernière bougie clôturée
        try:
            equity = self._account_value()
        except Exception as e:
            logger.error("equity illisible avant entrées: %r — sweep écourté", e)
            self.state["last_sweep_hour"] = hour
            self._save_state()
            return False

        gross = sum(p["notional"] for p in self.state["positions"].values())
        for sym in sorted(data):
            if sym in self.state["positions"]:
                continue
            closes = [c["close"] for c in data[sym]]
            r = rsi_series(closes, RSI_N)
            if not (r[-2] <= 30 < r[-1]):
                continue
            regime = filtered_regime(closes) if REGIME_FILTER else 1
            mult = REGIME_SIZE.get(regime, 1.0) if REGIME_FILTER else 1.0
            if mult <= 0:
                self.state["skipped"]["regime_calm"] += 1
                logger.info("%s: signal ignoré — régime calme (aucun edge brut)",
                            sym)
                continue
            if len(self.state["positions"]) >= MAX_CONCURRENT:
                self.state["skipped"]["slots"] += 1
                logger.info("%s: signal sauté — %d slots occupés",
                            sym, MAX_CONCURRENT)
                continue
            notional = equity * NOTIONAL_PCT * mult
            if gross + notional > equity * MAX_GROSS_PCT:
                self.state["skipped"]["gross_cap"] += 1
                logger.info("%s: signal sauté — plafond d'exposition "
                            "(%.0f$ + %.0f$ > %.0f$)", sym, gross, notional,
                            equity * MAX_GROSS_PCT)
                continue
            entry = self._exec_open(sym, notional, ref[sym], regime, now_ms)
            if entry is None:
                continue
            self.state["positions"][sym] = entry
            gross += entry["notional"]
            logger.info("ENTRÉE %s %.4f @ %.6f (%.2f$, régime %d, mult %.2f)",
                        sym, entry["sz"], entry["entry"], entry["notional"],
                        regime, mult)

        self.state["last_sweep_hour"] = hour
        self._save_state()
        logger.info("sweep terminé — %d positions, réalisé %+.3f$, %d trades, "
                    "sauts %s", len(self.state["positions"]),
                    self.state["realized_usd"], self.state["n_trades"],
                    self.state["skipped"])
        return True
