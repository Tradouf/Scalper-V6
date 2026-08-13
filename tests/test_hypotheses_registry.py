"""
Tests du registre des hypothèses — anti multiple-testing au niveau stratégie.

Le registre n'a de valeur que si trois choses tiennent :

1. il est **append-only** — un candidat rejeté qui disparaît cesse de durcir le
   seuil du suivant, et la correction de Bonferroni devient un décor ;
2. le **seuil corrigé** est calculé, pas estimé de tête ;
3. le nombre de **tirages placebo** suit le seuil — en dessous de `1/α`,
   `placebo_gate` ne peut mathématiquement pas passer, et un candidat serait
   condamné par une erreur d'arithmétique plutôt que par ses résultats.

Ces tests sont la différence entre une règle écrite et une règle appliquée.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REGISTRY = Path(__file__).resolve().parent.parent / "hypotheses" / "REGISTRY.md"


def entries() -> list:
    """Titres des entrées du registre, dans l'ordre d'écriture."""
    if not REGISTRY.exists():
        return []
    return re.findall(r"^## Entrée n°(\d+) — (.+)$", REGISTRY.read_text(encoding="utf-8"),
                      re.MULTILINE)


def corrected_alpha(n: int, base: float = 0.05) -> float:
    """Bonferroni : α / n, où n compte le candidat courant."""
    if n < 1:
        raise ValueError("n doit être ≥ 1")
    return base / n


def min_placebo_draws(alpha: float) -> int:
    """Tirages minimum pour que le gate PUISSE passer.

    `placebo_gate` rend p = (ge + 1) / (tirages + 1) : la p-value la plus petite
    atteignable est donc 1/(tirages+1). Il faut strictement plus de 1/α − 1
    tirages, sinon aucun résultat, même parfait, ne descend sous le seuil.
    """
    return int(1.0 / alpha)


def test_registre_existe_et_contient_au_moins_une_entree():
    assert REGISTRY.exists(), "le registre doit exister AVANT tout backtest de candidat"
    assert entries(), "au moins une entrée attendue"


def test_entrees_numerotees_sans_trou_ni_doublon():
    """Une numérotation trouée signale une entrée supprimée — donc un candidat
    testé qui ne durcit plus le seuil des suivants."""
    nums = [int(n) for n, _ in entries()]
    assert nums == sorted(nums), "les entrées doivent rester dans l'ordre d'écriture"
    assert nums == list(range(1, len(nums) + 1)), (
        f"numérotation trouée ou dupliquée: {nums} — une entrée a-t-elle été supprimée ?")


def test_entree_1_est_le_confluence_agent_rejete():
    text = REGISTRY.read_text(encoding="utf-8")
    assert "Entrée n°1 — ConfluenceAgent multi-timeframe" in text
    section = text.split("## Entrée n°1")[1].split("## Entrée n°2")[0]
    assert "REJETÉ" in section
    assert "0,42" in section and "0,61" in section, "les p-values placebo doivent figurer"


def test_chaque_entree_porte_les_champs_obligatoires():
    """Sans justification économique ni hash de config, une entrée ne prouve
    rien : ni que l'hypothèse précédait les données, ni qu'on sait qui paie."""
    text = REGISTRY.read_text(encoding="utf-8")
    blocks = text.split("## Entrée n°")[1:]
    assert blocks, "aucune entrée trouvée"
    for block in blocks:
        titre = block.splitlines()[0]
        for champ in ("Justification économique", "Config gelée", "Candidats à ce jour",
                      "Seuil placebo appliqué", "VERDICT"):
            assert champ in block, f"entrée '{titre}': champ obligatoire manquant — {champ}"
        assert re.search(r"sha256\s*=\s*[0-9a-f]{16,}", block), (
            f"entrée '{titre}': hash de configuration absent ou mal formé")


@pytest.mark.parametrize("n,expected", [(1, 0.05), (2, 0.025), (4, 0.0125), (5, 0.01)])
def test_correction_bonferroni(n, expected):
    assert corrected_alpha(n) == pytest.approx(expected)


@pytest.mark.parametrize("n,draws", [(1, 20), (2, 40), (3, 60), (5, 100)])
def test_tirages_minimum_suivent_le_seuil(n, draws):
    """Le gate ne PEUT pas passer sous 1/α tirages, quelle que soit la stratégie."""
    assert min_placebo_draws(corrected_alpha(n)) == draws


def test_table_du_registre_est_arithmetiquement_juste():
    """La table du registre est lue par des humains pressés : si elle ment, la
    correction ne sera pas appliquée."""
    text = REGISTRY.read_text(encoding="utf-8")
    rows = re.findall(r"^\| (\d) \| (0,\d+) \| (\d+) \|$", text, re.MULTILINE)
    assert rows, "table des seuils introuvable dans le registre"
    for n_str, alpha_str, draws_str in rows:
        n = int(n_str)
        alpha = float(alpha_str.replace(",", "."))
        assert alpha == pytest.approx(corrected_alpha(n), abs=5e-5), (
            f"n={n}: la table annonce α={alpha}, or 0,05/{n} = {corrected_alpha(n):.4f}")
        assert int(draws_str) == min_placebo_draws(corrected_alpha(n)), (
            f"n={n}: tirages annoncés {draws_str}, requis {min_placebo_draws(alpha)}")


def test_le_seuil_minimal_de_placebo_gate_est_bien_celui_quon_croit():
    """Vérifie la contrainte contre l'implémentation réelle, pas contre un
    souvenir de sa docstring."""
    import placebo_gate

    source = Path(placebo_gate.__file__).read_text(encoding="utf-8")
    assert "1.0 / (n_placebo + 1) >= alpha" in source, (
        "placebo_gate a changé sa règle de p minimale — revoir la table du registre")


def test_confluence_est_bloque_au_deploiement():
    """Le verdict de rejet doit être exécutable, pas seulement documenté."""
    from confluence import config as config_mod
    from confluence.agent import ConfluenceAgent, DeploymentBlocked

    cfg = config_mod.load()
    ConfluenceAgent(cfg)                       # étude/backtest : autorisé
    with pytest.raises(DeploymentBlocked):
        ConfluenceAgent(cfg, live=True)        # passage d'ordre : interdit


def test_verdict_et_marqueur_de_blocage_presents():
    root = REGISTRY.parent.parent
    assert (root / "confluence" / "VERDICT.md").exists()
    assert (root / "confluence" / "DEPLOY_BLOCKED").exists()


# ── Règle de méthode : on ne retouche pas les paramètres pour faire passer ────

def test_une_config_modifiee_apres_gel_est_detectee(tmp_path, monkeypatch):
    """« Si ça rejette, ça rejette. »

    Ajuster un seuil après avoir vu un résultat puis relancer est du
    multiple-testing : la p-value obtenue ne veut plus rien dire, et rien dans
    le rapport ne le signalerait. La mémoire humaine est un mauvais gardien —
    trois semaines plus tard, personne ne sait si `k_stop` a bougé. Le hash, si.
    """
    from confluence.run import check_config_frozen

    assert check_config_frozen(None) is None, "la config du dépôt doit être intacte"

    modifiee = tmp_path / "confluence.yaml"
    modifiee.write_text(
        Path("config/confluence.yaml").read_text(encoding="utf-8") + "\n# retouche\n",
        encoding="utf-8")
    problem = check_config_frozen(modifiee)
    assert problem is not None
    assert "CHANGÉ" in problem and "multiple-testing" in problem


def test_absence_de_gel_est_signalee(tmp_path, monkeypatch):
    """Pas de config gelée = le gate placebo ne prouve rien."""
    import confluence.run as run_mod

    monkeypatch.setattr(run_mod, "_frozen_config_hash", lambda: None)
    problem = run_mod.check_config_frozen(None)
    assert problem is not None and "geler la config AVANT" in problem


def test_grid_est_bloque_au_deploiement():
    """Candidat n°2 rejeté : même traitement que le n°1, blocage exécutable."""
    from grid import config as grid_config
    from grid.agent import GridAgent, GridDeploymentBlocked
    from grid.build import RangeSpec, build_grid

    cfg = grid_config.load()
    plan = build_grid(cfg, RangeSpec(60_000.0, 64_000.0, 62_000.0, 4_000.0),
                      atr_1h=400.0, atr_15m=150.0, equity=10_000.0)
    GridAgent(cfg, plan, 10_000.0, 0)                     # étude : autorisé
    with pytest.raises(GridDeploymentBlocked):
        GridAgent(cfg, plan, 10_000.0, 0, live=True)      # ordre : interdit


def test_entree_2_porte_son_verdict():
    text = REGISTRY.read_text(encoding="utf-8")
    section = text.split("## Entrée n°2")[1]
    assert "REJETÉ" in section
    assert "0,634" in section, "la p-value placebo doit figurer"
    assert "Runs effectués" in section, "le nombre de runs doit être consigné"
