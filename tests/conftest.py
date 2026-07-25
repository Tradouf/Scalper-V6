"""Garde-fous communs à toute la suite de tests.

Règle absolue (incident 2026-07-12, pollution de l'état prod par les tests) :
les tests ne touchent JAMAIS un répertoire d'état réel. Le cache OHLCV partagé
(hl_ohlcv_cache) est donc redirigé vers un tmp_path — sinon un test passant par
fetch_ohlcv avec des données mockées écrirait de fausses bougies que les bots
réels reliraient.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_ohlcv_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HL_OHLCV_CACHE_DIR", str(tmp_path / "ohlcv_cache"))
