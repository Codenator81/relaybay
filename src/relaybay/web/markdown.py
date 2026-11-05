from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, unquote

from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml
from markdown_it.rules_core import StateCore
from markdown_it.token import Token
from markupsafe import Markup

from relaybay.domain.digest import doc_link, doc_link_spans
from relaybay.domain.memory import DocRef, sections

# Reports come from agents and are untrusted: raw HTML stays escaped; markdown-it's default link
# validation already refuses javascript:, vbscript: and file: URLs.
_md = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")
_doc_md = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")


def _heading_open(self: Any, tokens: list[Any], idx: int, options: Any, env: dict) -> str:
    """A heading gets the anchor of the `domain.memory.sections` entry that starts on its line;
    one the sections do not count (setext, in a quote, in a fence the two parsers read
    differently) gets no id, so a link or an edit never lands on another section."""
    token = tokens[idx]
    anchor = env.get("anchors", {}).get(token.map[0]) if token.map else None
    if anchor:
        token.attrSet("id", anchor)
    return str(self.renderToken(tokens, idx, options, env))


def _heading_close(self: Any, tokens: list[Any], idx: int, options: Any, env: dict) -> str:
    """With `edit_url` in env, a heading with a section anchor ends with a link that edits
    that section."""
    open_token = tokens[idx - 2]
    anchor = open_token.attrGet("id")
    edit = env.get("edit_url")
    closing = str(self.renderToken(tokens, idx, options, env))
    if not edit or not anchor:
        return closing
    href = escapeHtml(f"{edit}?section={quote(anchor)}")
    return f' <a class="section-edit" href="{href}">edit</a>{closing}'


_doc_md.add_render_rule("heading_open", _heading_open)
_doc_md.add_render_rule("heading_close", _heading_close)

# Fixed markup only: the reason is one of DocRef's own strings, never text from the source.
_MARKS = {
    reason: f'<span class="doc-link-mark">{reason}</span>'
    for reason in ("no such section", "no such document")
}


def _link_tokens(ref: DocRef, label: str) -> list[Token]:
    """A board link for a document link written as plain text (or the text with a mark when
    the document does not exist)."""
    out = []
    if ref.href:
        out.append(Token("link_open", "a", 1, attrs={"href": ref.href, "class": "doc-link"}))
    out.append(Token("text", "", 0, content=label))
    if ref.href:
        out.append(Token("link_close", "a", -1))
    if ref.broken:
        out.append(Token("html_inline", "", 0, content=" " + _MARKS[ref.broken]))
    return out


def _doc_links(state: StateCore) -> None:
    """`relaybay://doc/...` links, written as text or as a markdown link, point at the board
    document; only links resolved beforehand (env `doc_refs`) change, nothing else is read."""
    refs: Mapping[str, DocRef] | None = state.env.get("doc_refs")
    if not refs:
        return
    for block in state.tokens:
        if block.type != "inline" or not block.children:
            continue
        children: list[Token] = []
        # Per open link: its mark, and whether the link itself is dropped (no such document).
        stack: list[tuple[str | None, bool]] = []
        for token in block.children:
            if token.type == "link_open":
                link = doc_link(unquote(token.attrGet("href") or ""))
                ref = refs.get(link.uri) if link else None
                if ref is not None and ref.href:
                    token.attrSet("href", ref.href)
                    token.attrSet("class", "doc-link")
                stack.append((ref.broken if ref else None, ref is not None and not ref.href))
                if not stack[-1][1]:
                    children.append(token)
            elif token.type == "link_close" and stack:
                broken, dropped = stack.pop()
                if not dropped:
                    children.append(token)
                if broken:
                    children.append(Token("html_inline", "", 0, content=" " + _MARKS[broken]))
            elif token.type == "text" and not stack:
                children.extend(_split_text(token, refs))
            else:
                children.append(token)
        block.children = children


def _split_text(token: Token, refs: Mapping[str, DocRef]) -> list[Token]:
    text = token.content
    out: list[Token] = []
    at = 0
    for start, end, link in doc_link_spans(text):
        ref = refs.get(link.uri)
        if ref is None:
            continue
        if start > at:
            out.append(Token("text", "", 0, content=text[at:start]))
        out.extend(_link_tokens(ref, text[start:end]))
        at = end
    if not out:
        return [token]
    if at < len(text):
        out.append(Token("text", "", 0, content=text[at:]))
    return out


_md.core.ruler.push("doc_links", _doc_links)
_doc_md.core.ruler.push("doc_links", _doc_links)


def render_markdown(text: str | None, doc_refs: Mapping[str, DocRef] | None = None) -> Markup:
    """Agent markdown; with `doc_refs`, its document links point at the board."""
    return Markup(_md.render(text or "", {"doc_refs": doc_refs or {}}))


def render_document(text: str | None, edit_url: str | None = None) -> Markup:
    """A project document: like `render_markdown`, with ids on headings for section links, and
    with `edit_url` an edit link per top-level heading."""
    anchors = {s.start: s.anchor for s in sections(text or "")}
    return Markup(_doc_md.render(text or "", {"edit_url": edit_url, "anchors": anchors}))
