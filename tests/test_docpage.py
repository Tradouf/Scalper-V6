"""Tests du rendu Markdown servi par les tableaux de bord."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docpage import md_to_html, render_doc  # noqa: E402


def test_headings_and_paragraphs():
    h = md_to_html("# Titre\n\nUn texte\nsur deux lignes.\n")
    assert "<h1>Titre</h1>" in h
    assert "<p>Un texte sur deux lignes.</p>" in h


def test_table_is_rendered():
    h = md_to_html("| A | B |\n|---|---|\n| 1 | 2 |\n")
    assert "<th>A</th>" in h and "<th>B</th>" in h
    assert "<td>1</td>" in h and "<td>2</td>" in h
    assert h.count("<table>") == 1 and h.count("</table>") == 1


def test_list_items_and_continuation():
    h = md_to_html("- premier point\n  suite du premier\n- second point\n")
    assert h.count("<li>") == 2
    assert "premier point suite du premier" in h


def test_inline_formatting():
    h = md_to_html("Du **gras**, du `code` et un [lien](https://x.test).")
    assert "<strong>gras</strong>" in h
    assert "<code>code</code>" in h
    assert '<a href="https://x.test">lien</a>' in h


def test_html_in_document_is_escaped():
    """Un document est une donnée : il ne doit jamais injecter de HTML."""
    h = md_to_html("Texte <script>alert(1)</script> et <b>gras</b>.")
    assert "<script>" not in h
    assert "&lt;script&gt;" in h
    assert "<b>" not in h


def test_link_target_is_escaped():
    h = md_to_html('[x](https://a.test/?q="onerror=)')
    assert 'onerror' not in h.split('href="')[1].split('"')[0] or "&quot;" in h


def test_horizontal_rule_and_blockquote():
    h = md_to_html("---\n\n> une citation\n")
    assert "<hr>" in h
    assert "<blockquote>une citation</blockquote>" in h


def test_render_doc_missing_file_is_explicit(tmp_path):
    out = render_doc(tmp_path / "absent.md").decode()
    assert "Document indisponible" in out
    assert "retour au tableau de bord" in out


def test_render_real_functional_analysis():
    """Le vrai document doit se rendre sans perdre ses tableaux."""
    doc = Path(__file__).resolve().parent.parent / "ANALYSE_FONCTIONNELLE.md"
    out = render_doc(doc).decode()
    assert "<table>" in out
    assert "Ricochet" in out and "XSMom" in out
    assert "<h1>" in out


# ── Le document est réellement servi par les deux tableaux de bord ──────────

def test_both_dashboards_expose_the_doc_route():
    import inspect
    from rsimr import dashboard as R
    from xsmom import dashboard as X
    for mod in (R, X):
        src = inspect.getsource(mod.Handler.do_GET)
        assert '"/doc"' in src, f"{mod.__name__} ne sert pas /doc"
        assert "render_doc" in src
        assert 'href="/doc"' in mod.INDEX_HTML, f"{mod.__name__} : lien absent"


def test_both_dashboards_point_at_the_same_document():
    from rsimr import dashboard as R
    from xsmom import dashboard as X
    assert R.DOC_FILE == X.DOC_FILE
    assert R.DOC_FILE.name == "ANALYSE_FONCTIONNELLE.md"
