from typing import Any

from markdown_it import MarkdownIt
from markupsafe import Markup

from relaybay.domain.memory import heading_anchor

# Reports come from agents and are untrusted: raw HTML stays escaped; markdown-it's default link
# validation already refuses javascript:, vbscript: and file: URLs.
_md = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")
_doc_md = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")


def _heading_open(self: Any, tokens: list[Any], idx: int, options: Any, env: dict) -> str:
    """Top-level `#` headings get the section anchors of `domain.memory.sections` as ids
    (same slug rule, repeats numbered `-2`, `-3`…)."""
    token = tokens[idx]
    if token.markup.startswith("#") and token.level == 0:
        seen: set[str] = env.setdefault("anchors", set())
        base = anchor = heading_anchor(tokens[idx + 1].content)
        n = 1
        while anchor in seen:
            n += 1
            anchor = f"{base}-{n}"
        seen.add(anchor)
        token.attrSet("id", anchor)
    return str(self.renderToken(tokens, idx, options, env))


_doc_md.add_render_rule("heading_open", _heading_open)


def render_markdown(text: str | None) -> Markup:
    return Markup(_md.render(text or ""))


def render_document(text: str | None) -> Markup:
    """A project document: like `render_markdown`, with ids on headings for section links."""
    return Markup(_doc_md.render(text or "", {}))
