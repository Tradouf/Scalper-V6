"""Point d'entrée LLMBot."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from llmbot import config
from llmbot.live import LLMLiveTrader


def _make_client():
    from hyperliquid_client import HyperliquidClient
    key = os.environ.get(config.ENV_PRIVATE_KEY)
    if not key:
        raise RuntimeError(f"{config.ENV_PRIVATE_KEY} manquant")
    addr = os.environ.get(config.ENV_ACCOUNT_ADDRESS) or None
    client = HyperliquidClient(wallet_key=key)
    if addr and client.wallet_address and addr.lower() != client.wallet_address.lower():
        client._init_exchange(key, account_address=addr)
    for other_key, other_addr in [
        ("HL_PRIVATE_KEY", "HL_ACCOUNT_ADDRESS"),
        ("HL2_PRIVATE_KEY", "HL2_ACCOUNT_ADDRESS"),
    ]:
        ok = os.environ.get(other_key, "")
        oa = os.environ.get(other_addr, "")
        if ok and ok.lower() == key.lower():
            raise RuntimeError(f"{config.ENV_PRIVATE_KEY} identique à {other_key}")
        if oa and client.wallet_address and oa.lower() == client.wallet_address.lower():
            raise RuntimeError(f"Wallet identique à {other_addr}")
    return client


def main() -> int:
    parser = argparse.ArgumentParser(description="LLMBot — trading Hyperliquid piloté par LLM")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    logger = logging.getLogger("sdm.llmbot")

    client = None
    if not config.DRY_RUN:
        client = _make_client()
        logger.info("Wallet LLMBot: %s", client.wallet_address)
    else:
        logger.info("DRY-RUN — pas de wallet requis (LLMBOT_DRY_RUN=0 pour live)")

    LLMLiveTrader(client=client).run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())