from markdown_it import MarkdownIt
from markupsafe import Markup

# Reports come from agents and are untrusted: raw HTML stays escaped; markdown-it's default link
# validation already refuses javascript:, vbscript: and file: URLs.
_md = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")


def render_markdown(text: str | None) -> Markup:
    return Markup(_md.render(text or ""))
