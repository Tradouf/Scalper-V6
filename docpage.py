"""
Rendu d'un document Markdown en page HTML, pour les tableaux de bord.

Un lien vers un fichier local ne marche pas depuis une page servie en HTTP
(les navigateurs bloquent `file://`), et aucune bibliothèque Markdown n'est
installée. Ce module fait donc le strict nécessaire pour rendre lisible
`ANALYSE_FONCTIONNELLE.md` : titres, tableaux, listes, gras, code, liens,
citations et séparateurs — servi par les dashboards à l'adresse `/doc`.

Tout le texte est échappé AVANT toute transformation : un document est une
donnée, il ne doit jamais pouvoir injecter du HTML dans la page.
"""
from __future__ import annotations

import html
import re
from pathlib import Path
from typing import List

_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITAL = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\*)")
_CODE = re.compile(r"`([^`]+)`")


def _inline(text: str) -> str:
    """Échappe puis applique le formatage de ligne (code en dernier protégé)."""
    out = html.escape(text, quote=False)
    out = _CODE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", out)
    out = _ITAL.sub(lambda m: f"<em>{m.group(1)}</em>", out)
    out = _LINK.sub(
        lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>',
        out)
    return out


def _is_table_sep(line: str) -> bool:
    s = line.strip()
    return bool(s) and set(s) <= set("|-: ") and "-" in s and "|" in s


def _cells(line: str) -> List[str]:
    s = line.strip().strip("|")
    return [c.strip() for c in s.split("|")]


def md_to_html(md: str) -> str:
    """Convertit le sous-ensemble Markdown utilisé par nos documents."""
    lines = md.splitlines()
    out: List[str] = []
    i = 0
    list_open = False

    def close_list():
        nonlocal list_open
        if list_open:
            out.append("</ul>")
            list_open = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # tableau : en-tête + ligne de séparation
        if (stripped.startswith("|") and i + 1 < len(lines)
                and _is_table_sep(lines[i + 1])):
            close_list()
            head = _cells(stripped)
            out.append("<table><tr>"
                       + "".join(f"<th>{_inline(c)}</th>" for c in head)
                       + "</tr>")
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                out.append("<tr>" + "".join(
                    f"<td>{_inline(c)}</td>" for c in _cells(lines[i])) + "</tr>")
                i += 1
            out.append("</table>")
            continue

        if not stripped:
            close_list()
            i += 1
            continue

        if stripped.startswith("#"):
            close_list()
            level = len(stripped) - len(stripped.lstrip("#"))
            level = min(level, 6)
            out.append(f"<h{level}>{_inline(stripped[level:].strip())}</h{level}>")
            i += 1
            continue

        if set(stripped) <= {"-", "*", "_"} and len(stripped) >= 3:
            close_list()
            out.append("<hr>")
            i += 1
            continue

        if stripped.startswith(("- ", "* ")):
            if not list_open:
                out.append("<ul>")
                list_open = True
            item = [stripped[2:]]
            i += 1
            # continuation indentée d'un même point
            while (i < len(lines) and lines[i].startswith(("  ", "\t"))
                   and lines[i].strip()
                   and not lines[i].strip().startswith(("- ", "* ", "|"))):
                item.append(lines[i].strip())
                i += 1
            out.append(f"<li>{_inline(' '.join(item))}</li>")
            continue

        if stripped.startswith(">"):
            close_list()
            out.append(f"<blockquote>{_inline(stripped.lstrip('> '))}</blockquote>")
            i += 1
            continue

        # paragraphe : on recolle les lignes jusqu'au prochain blanc
        close_list()
        para = [stripped]
        i += 1
        while i < len(lines) and lines[i].strip() and not (
                lines[i].strip().startswith(("#", "- ", "* ", "|", ">"))
                or (set(lines[i].strip()) <= {"-", "*", "_"}
                    and len(lines[i].strip()) >= 3)):
            para.append(lines[i].strip())
            i += 1
        out.append(f"<p>{_inline(' '.join(para))}</p>")

    close_list()
    return "\n".join(out)


_PAGE = """<!doctype html>
<meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{{--bg:#0f1115;--card:#171a21;--fg:#e6e8ee;--mut:#9aa3b2;--line:#242833;
--acc:#6ea8fe}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.65 system-ui,sans-serif}}
.wrap{{max-width:820px;margin:0 auto;padding:24px 20px 80px}}
a{{color:var(--acc)}}
h1{{font-size:26px;margin:8px 0 16px;line-height:1.25}}
h2{{font-size:20px;margin:34px 0 10px;padding-top:14px;
border-top:1px solid var(--line)}}
h3{{font-size:16px;margin:22px 0 6px;color:#cfd6e4}}
p{{margin:10px 0}}
ul{{margin:10px 0;padding-left:22px}} li{{margin:4px 0}}
code{{background:#1c2029;padding:1px 5px;border-radius:4px;font-size:.9em}}
hr{{border:0;border-top:1px solid var(--line);margin:26px 0}}
blockquote{{margin:12px 0;padding:8px 14px;border-left:3px solid var(--line);
color:var(--mut)}}
table{{width:100%;border-collapse:collapse;margin:14px 0;font-size:14px;
display:block;overflow-x:auto}}
th,td{{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);
vertical-align:top}}
th{{color:var(--mut);font-weight:600;white-space:nowrap}}
.back{{display:inline-block;margin-bottom:6px;color:var(--mut);
text-decoration:none;font-size:14px}}
.back:hover{{color:var(--acc)}}
</style>
<div class="wrap"><a class="back" href="/">← retour au tableau de bord</a>
{body}</div>
"""


def render_doc(path: Path, title: str = "Analyse fonctionnelle") -> bytes:
    """Page HTML complète pour un fichier Markdown ; message clair s'il manque."""
    try:
        md = Path(path).read_text(encoding="utf-8")
    except Exception as e:
        body = (f"<h1>Document indisponible</h1><p>Impossible de lire "
                f"<code>{html.escape(str(path))}</code> : {html.escape(str(e))}</p>")
        return _PAGE.format(title=title, body=body).encode("utf-8")
    return _PAGE.format(title=title, body=md_to_html(md)).encode("utf-8")
