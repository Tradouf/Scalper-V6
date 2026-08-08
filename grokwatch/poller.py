"""
Poller IMAP — surveille une boîte mail et ingère les signaux Grok.

Config par variables d'environnement (.env) :
    GROKWATCH_IMAP_HOST      ex: imap.gmail.com, ou 127.0.0.1 (Proton Bridge)
    GROKWATCH_IMAP_PORT      993 par défaut (SSL) ; 1143 + STARTTLS pour Bridge
    GROKWATCH_IMAP_USER
    GROKWATCH_IMAP_PASSWORD  mot de passe d'application, PAS le mdp du compte
    GROKWATCH_IMAP_FOLDER    INBOX par défaut
    GROKWATCH_SENDER         filtre expéditeur (sous-chaîne, optionnel)
    GROKWATCH_SUBJECT        filtre sujet (sous-chaîne, défaut: "quid is ready")
    GROKWATCH_POLL_SEC       intervalle de scrutation (défaut 300 s)

Le dernier UID traité est persisté dans state/poller.json : on ne retraite
jamais un mail, même après redémarrage (et le store dédoublonne par contenu).

    python -m grokwatch.poller           # boucle permanente
    python -m grokwatch.poller --once    # un seul passage (debug/cron)
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import json
import logging
import os
import sys
import time
from typing import List, Optional, Tuple

from grokwatch.ingest import ingest_text
from grokwatch.store import _state_dir

logger = logging.getLogger("sdm.grokwatch.poller")


def _cfg() -> dict:
    return {
        "host": os.environ.get("GROKWATCH_IMAP_HOST", "").strip(),
        "port": int(os.environ.get("GROKWATCH_IMAP_PORT", "993")),
        "user": os.environ.get("GROKWATCH_IMAP_USER", "").strip(),
        "password": os.environ.get("GROKWATCH_IMAP_PASSWORD", ""),
        "folder": os.environ.get("GROKWATCH_IMAP_FOLDER", "INBOX"),
        "sender": os.environ.get("GROKWATCH_SENDER", "").strip().lower(),
        "subject": os.environ.get("GROKWATCH_SUBJECT", "quid is ready").strip().lower(),
        "poll_sec": float(os.environ.get("GROKWATCH_POLL_SEC", "300")),
    }


def _uid_path():
    return _state_dir() / "poller.json"


def _load_last_uid() -> int:
    try:
        with open(_uid_path(), "r", encoding="utf-8") as f:
            return int(json.load(f).get("last_uid", 0))
    except Exception:
        return 0


def _save_last_uid(uid: int) -> None:
    with open(_uid_path(), "w", encoding="utf-8") as f:
        json.dump({"last_uid": uid}, f)


def _body_text(msg: email.message.Message) -> str:
    """text/plain de préférence, sinon text/html brut (le parser strippe)."""
    plain, html_part = [], []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace")
        (plain if ctype == "text/plain" else html_part).append(text)
    return "\n".join(plain) or "\n".join(html_part)


def _decode_header(raw: Optional[str]) -> str:
    if not raw:
        return ""
    out = []
    for chunk, charset in email.header.decode_header(raw):
        if isinstance(chunk, bytes):
            out.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


def _connect(cfg: dict) -> imaplib.IMAP4:
    if cfg["port"] == 993:
        conn = imaplib.IMAP4_SSL(cfg["host"], cfg["port"])
    else:
        # Proton Mail Bridge écoute en local sans SSL (STARTTLS)
        conn = imaplib.IMAP4(cfg["host"], cfg["port"])
        conn.starttls()
    conn.login(cfg["user"], cfg["password"])
    return conn


def poll_once(cfg: Optional[dict] = None) -> int:
    """Un passage : retourne le nombre de signaux ingérés."""
    cfg = cfg or _cfg()
    last_uid = _load_last_uid()
    conn = _connect(cfg)
    ingested = 0
    try:
        conn.select(cfg["folder"], readonly=True)
        status, data = conn.uid("search", None, f"UID {last_uid + 1}:*")
        if status != "OK":
            logger.warning("IMAP search KO: %s", status)
            return 0
        uids = [int(u) for u in data[0].split()] if data and data[0] else []
        # « UID n:* » renvoie toujours le dernier mail même si uid <= n
        uids = [u for u in uids if u > last_uid]
        for uid in sorted(uids):
            status, msg_data = conn.uid("fetch", str(uid), "(RFC822)")
            if status != "OK" or not msg_data or msg_data[0] is None:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            sender = _decode_header(msg.get("From")).lower()
            subject = _decode_header(msg.get("Subject")).lower()
            _save_last_uid(uid)
            last_uid = uid
            if cfg["sender"] and cfg["sender"] not in sender:
                continue
            if cfg["subject"] and cfg["subject"] not in subject:
                continue
            ts = None
            try:
                dt = email.utils.parsedate_to_datetime(msg.get("Date"))
                ts = dt.timestamp()
            except Exception:
                pass
            if ingest_text(_body_text(msg), received_ts=ts,
                           source=f"imap:{uid}") is not None:
                ingested += 1
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return ingested


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    cfg = _cfg()
    if not (cfg["host"] and cfg["user"] and cfg["password"]):
        print("Config IMAP manquante — renseigner GROKWATCH_IMAP_HOST/USER/PASSWORD "
              "dans .env (voir grokwatch/README.md).")
        return 2
    once = "--once" in sys.argv
    logger.info("Poller GrokWatch démarré — %s:%s dossier=%s intervalle=%.0fs%s",
                cfg["host"], cfg["port"], cfg["folder"], cfg["poll_sec"],
                " (passage unique)" if once else "")
    while True:
        try:
            n = poll_once(cfg)
            if n:
                logger.info("%d signal(aux) ingéré(s)", n)
        except Exception as e:
            logger.warning("poll_once: %r", e)
        if once:
            return 0
        time.sleep(cfg["poll_sec"])


if __name__ == "__main__":
    sys.exit(main())
