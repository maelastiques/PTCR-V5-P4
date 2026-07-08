"""Markdown-to-HTML rendering for the Maelectrix chat.

This module has no third-party dependencies (the KiCad bundled Python does not
ship ``markdown`` or ``pygments``).  It provides a small but robust Markdown
subset renderer and a themed HTML document builder used by the WebView-based
chat window.

Security note: every piece of model/user provided text is HTML-escaped before
any Markdown transformation is applied, and only ``http``, ``https`` and
``mailto`` link targets are emitted.  This prevents HTML/script injection in
the WebView.
"""

from __future__ import annotations

import base64
import html
import os
import re
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

def build_theme(is_dark: bool) -> Dict[str, str]:
    """Return a palette (hex colours) for the given light/dark preference."""
    if is_dark:
        palette = {
            "is_dark": "1",
            "app_bg": "#17181c",
            "surface": "#26282e",
            "surface_2": "#2f3138",
            "user_grad_a": "#3b82f6",
            "user_grad_b": "#2563eb",
            "user_fg": "#ffffff",
            "text": "#e7e9ee",
            "muted": "#9aa0ab",
            "accent": "#5b9bff",
            "border": "#34363d",
            "code_bg": "#1b1d23",
            "code_fg": "#e7e9ee",
            "pre_bg": "#0f1116",
            "pre_head": "#1b1d23",
            "pre_fg": "#e6edf3",
            "quote_bg": "#20222840",
            "table_head": "#2f3138",
            "shadow": "rgba(0,0,0,0.35)",
        }
    else:
        palette = {
            "is_dark": "0",
            "app_bg": "#f4f5f7",
            "surface": "#ffffff",
            "surface_2": "#f5f7f9",
            "user_grad_a": "#4a90ff",
            "user_grad_b": "#2f6fed",
            "user_fg": "#ffffff",
            "text": "#1c1e21",
            "muted": "#6b7280",
            "accent": "#2f6fed",
            "border": "#e5e8ec",
            "code_bg": "#eef0f3",
            "code_fg": "#c7254e",
            "pre_bg": "#0f1116",
            "pre_head": "#1b1d23",
            "pre_fg": "#e6edf3",
            "quote_bg": "#f5f7f9",
            "table_head": "#f0f2f5",
            "shadow": "rgba(15,23,42,0.08)",
        }
    # Populated by the caller (chat_window) with base64 data URIs when available.
    palette["bg_image"] = "none"
    palette["avatar_src"] = ""
    return palette


# ---------------------------------------------------------------------------
# Inline Markdown
# ---------------------------------------------------------------------------

_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<![\*_])\*(?!\s)(.+?)(?<!\s)\*(?![\*])|(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", re.DOTALL)
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"([^\"]*)\")?\)")
_AUTOLINK_RE = re.compile(r"(?<![\">=])(https?://[^\s<>()]+[^\s<>().,;:!?])")

_SAFE_SCHEMES = ("http://", "https://", "mailto:")
_NAV_SCHEMES  = ("pcb://", "sch://")

# SVG icons for nav buttons (inline, currentColor)
_PCB_ICON = ('<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"'
             ' stroke-linecap="round">'
             '<rect x="2" y="2" width="12" height="12" rx="2"/>'
             '<circle cx="5.5" cy="5.5" r=".9" fill="currentColor" stroke="none"/>'
             '<circle cx="10.5" cy="10.5" r=".9" fill="currentColor" stroke="none"/>'
             '<line x1="5.5" y1="5.5" x2="10.5" y2="5.5"/>'
             '<line x1="10.5" y1="5.5" x2="10.5" y2="10.5"/>'
             '</svg>')
_SCH_ICON = ('<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"'
             ' stroke-linecap="round">'
             '<line x1="1" y1="8" x2="5" y2="8"/>'
             '<rect x="5" y="4.5" width="6" height="7" rx="1"/>'
             '<line x1="11" y1="8" x2="15" y2="8"/>'
             '</svg>')


def _nav_url(raw_url: str):
    """Convert pcb:// / sch:// to (app_url, css_class, icon) or None."""
    if raw_url.startswith("pcb://"):
        payload = raw_url[6:]  # e.g. "12.5,34.2" or "ref=U3"
        import urllib.parse as _up
        if payload.startswith("ref="):
            qs = payload
        elif "," in payload.split("?")[0]:
            coords = payload.split("?")[0]
            extra  = payload[len(coords):]
            parts = coords.split(",", 1)
            try:
                x = float(parts[0]); y = float(parts[1])
                qs = f"x={x}&y={y}" + ("&" + extra[1:] if extra else "")
            except ValueError:
                qs = "raw=" + _up.quote(payload)
        else:
            qs = "ref=" + _up.quote(payload)
        return "app://pcb-goto?" + qs, "nav-pcb", _PCB_ICON
    if raw_url.startswith("sch://"):
        import urllib.parse as _up
        ref = raw_url[6:]
        return "app://sch-goto?ref=" + _up.quote(ref), "nav-sch", _SCH_ICON
    return None


def _safe_url(url: str) -> Optional[str]:
    lowered = url.strip().lower()
    if lowered.startswith(_SAFE_SCHEMES):
        return url.strip()
    return None


def _render_inline(text: str) -> str:
    """Convert inline Markdown to HTML on a single logical line/paragraph."""
    placeholders: List[str] = []

    def _stash(fragment: str) -> str:
        placeholders.append(fragment)
        return "\x00{}\x00".format(len(placeholders) - 1)

    # 1. Protect code spans (their content must not be further processed).
    def _code_repl(match: "re.Match[str]") -> str:
        inner = html.escape(match.group(1), quote=False)
        return _stash('<code class="inline">{}</code>'.format(inner))

    text = _CODE_SPAN_RE.sub(_code_repl, text)

    # 2. Escape everything else.
    text = html.escape(text, quote=False)

    # 3. Links (before autolink so explicit links win).
    def _link_repl(match: "re.Match[str]") -> str:
        label = match.group(1)
        raw_url = html.unescape(match.group(2))
        # KiCad navigation links (pcb:// / sch://)
        nav = _nav_url(raw_url)
        if nav:
            app_url, css_cls, icon = nav
            escaped_url = html.escape(app_url, quote=True)
            return _stash(
                f'<a href="{escaped_url}" class="nav-btn {css_cls}">'
                f'{icon}{label}</a>'
            )
        safe = _safe_url(raw_url)
        if not safe:
            # Unsafe scheme: keep label only.
            return label
        href = html.escape(safe, quote=True)
        return _stash('<a href="{}">{}</a>'.format(href, label))

    text = _LINK_RE.sub(_link_repl, text)

    # 4. Bare URLs.
    def _auto_repl(match: "re.Match[str]") -> str:
        url = html.unescape(match.group(1))
        safe = _safe_url(url)
        if not safe:
            return match.group(0)
        href = html.escape(safe, quote=True)
        return _stash('<a href="{}">{}</a>'.format(href, match.group(1)))

    text = _AUTOLINK_RE.sub(_auto_repl, text)

    # 5. Emphasis.
    text = _BOLD_RE.sub(lambda m: "<strong>{}</strong>".format(m.group(1) or m.group(2)), text)
    text = _STRIKE_RE.sub(lambda m: "<del>{}</del>".format(m.group(1)), text)
    text = _ITALIC_RE.sub(lambda m: "<em>{}</em>".format(m.group(1) or m.group(2)), text)

    # 6. Restore protected fragments.
    def _restore(match: "re.Match[str]") -> str:
        return placeholders[int(match.group(1))]

    text = re.sub(r"\x00(\d+)\x00", _restore, text)
    return text


# ---------------------------------------------------------------------------
# Block-level Markdown
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)\s*([\w+-]*)\s*$")
_HR_RE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$")


def _split_table_row(line: str) -> List[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    # Split on unescaped pipes.
    cells = re.split(r"(?<!\\)\|", line)
    return [c.replace("\\|", "|").strip() for c in cells]


def _render_table(header: str, sep: str, body: List[str]) -> str:
    aligns: List[str] = []
    for spec in _split_table_row(sep):
        left = spec.startswith(":")
        right = spec.endswith(":")
        if left and right:
            aligns.append("center")
        elif right:
            aligns.append("right")
        elif left:
            aligns.append("left")
        else:
            aligns.append("")

    def _row(cells: List[str], tag: str) -> str:
        out = []
        for idx, cell in enumerate(cells):
            align = aligns[idx] if idx < len(aligns) else ""
            style = ' style="text-align:{}"'.format(align) if align else ""
            out.append("<{tag}{style}>{content}</{tag}>".format(
                tag=tag, style=style, content=_render_inline(cell)))
        return "<tr>{}</tr>".format("".join(out))

    head_html = _row(_split_table_row(header), "th")
    body_html = "".join(_row(_split_table_row(row), "td") for row in body)
    return '<table class="md-table"><thead>{}</thead><tbody>{}</tbody></table>'.format(
        head_html, body_html)


def _parse_list(lines: List[str], start: int) -> Tuple[str, int]:
    """Parse a (possibly nested) list starting at ``lines[start]``."""
    stack: List[Dict[str, object]] = []
    i = start

    def _close_to(indent: int) -> None:
        while stack and int(stack[-1]["indent"]) > indent:  # type: ignore[index]
            top = stack.pop()
            html_parts = top["items"]  # type: ignore[index]
            tag = "ol" if top["ordered"] else "ul"  # type: ignore[index]
            rendered = "<{tag}>{body}</{tag}>".format(tag=tag, body="".join(html_parts))  # type: ignore[arg-type]
            if stack:
                parent_items = stack[-1]["items"]  # type: ignore[index]
                parent_items[-1] = parent_items[-1][:-5] + rendered + "</li>"  # type: ignore[index]
            else:
                stack.append({"indent": -1, "ordered": top["ordered"], "items": [rendered], "closed": True})
                return

    result: List[str] = []
    while i < len(lines):
        match = _LIST_RE.match(lines[i])
        if not match:
            # Allow blank line inside a list only if the next line continues it.
            if lines[i].strip() == "" and i + 1 < len(lines) and _LIST_RE.match(lines[i + 1]):
                i += 1
                continue
            break
        indent = len(match.group(1).expandtabs(4))
        ordered = not match.group(2)[0] in "-*+"
        content = _render_inline(match.group(3))

        if not stack:
            stack.append({"indent": indent, "ordered": ordered, "items": []})
        elif indent > int(stack[-1]["indent"]):  # type: ignore[index]
            stack.append({"indent": indent, "ordered": ordered, "items": []})
        else:
            while len(stack) > 1 and indent < int(stack[-1]["indent"]):  # type: ignore[index]
                top = stack.pop()
                tag = "ol" if top["ordered"] else "ul"  # type: ignore[index]
                rendered = "<{tag}>{body}</{tag}>".format(
                    tag=tag, body="".join(top["items"]))  # type: ignore[arg-type]
                parent_items = stack[-1]["items"]  # type: ignore[index]
                parent_items[-1] = parent_items[-1][:-5] + rendered + "</li>"  # type: ignore[index]

        stack[-1]["items"].append("<li>{}</li>".format(content))  # type: ignore[index]
        i += 1

    # Collapse the remaining stack.
    while len(stack) > 1:
        top = stack.pop()
        tag = "ol" if top["ordered"] else "ul"  # type: ignore[index]
        rendered = "<{tag}>{body}</{tag}>".format(tag=tag, body="".join(top["items"]))  # type: ignore[arg-type]
        parent_items = stack[-1]["items"]  # type: ignore[index]
        parent_items[-1] = parent_items[-1][:-5] + rendered + "</li>"  # type: ignore[index]

    if stack:
        top = stack[0]
        tag = "ol" if top["ordered"] else "ul"  # type: ignore[index]
        result.append("<{tag}>{body}</{tag}>".format(tag=tag, body="".join(top["items"])))  # type: ignore[arg-type]

    return "".join(result), i


def markdown_to_html(text: str) -> str:
    """Convert a Markdown string into a safe HTML fragment."""
    if not text:
        return ""

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: List[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Fenced code block.
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            lang = fence.group(2) or ""
            code_lines: List[str] = []
            i += 1
            while i < n and not (lines[i].strip().startswith(marker[0] * 3)
                                 and set(lines[i].strip()) <= {marker[0]}):
                code_lines.append(lines[i])
                i += 1
            i += 1  # consume closing fence
            code_html = html.escape("\n".join(code_lines), quote=False)
            lang_label = html.escape(lang, quote=False) if lang else "code"
            out.append(
                '<div class="codeblock">'
                '<div class="codeblock-head"><span class="lang">{lang}</span></div>'
                '<pre><code>{code}</code></pre>'
                "</div>".format(lang=lang_label, code=code_html)
            )
            continue

        # Blank line.
        if stripped == "":
            i += 1
            continue

        # Horizontal rule.
        if _HR_RE.match(line):
            out.append("<hr/>")
            i += 1
            continue

        # Heading.
        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            out.append("<h{lvl}>{content}</h{lvl}>".format(
                lvl=level, content=_render_inline(heading.group(2).strip())))
            i += 1
            continue

        # Table (header + separator + rows).
        if "|" in line and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]):
            header = line
            sep = lines[i + 1]
            body: List[str] = []
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip() != "":
                body.append(lines[i])
                i += 1
            out.append(_render_table(header, sep, body))
            continue

        # Blockquote.
        if _QUOTE_RE.match(line):
            quote_lines: List[str] = []
            while i < n and _QUOTE_RE.match(lines[i]):
                quote_lines.append(_QUOTE_RE.match(lines[i]).group(1))  # type: ignore[union-attr]
                i += 1
            inner = markdown_to_html("\n".join(quote_lines))
            out.append("<blockquote>{}</blockquote>".format(inner))
            continue

        # List.
        if _LIST_RE.match(line):
            list_html, i = _parse_list(lines, i)
            out.append(list_html)
            continue

        # Paragraph: gather consecutive non-blank, non-structural lines.
        para: List[str] = []
        while i < n:
            cur = lines[i]
            cur_stripped = cur.strip()
            if cur_stripped == "":
                break
            if (_FENCE_RE.match(cur) or _HEADING_RE.match(cur) or _HR_RE.match(cur)
                    or _QUOTE_RE.match(cur) or _LIST_RE.match(cur)):
                break
            para.append(cur_stripped)
            i += 1
        if para:
            joined = "<br/>".join(_render_inline(p) for p in para)
            out.append("<p>{}</p>".format(joined))

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Conversation document
# ---------------------------------------------------------------------------

_AVATAR = {
    "user": "You",
    "assistant": "AI",
    "system": "SYS",
    "error": "!",
}


def _css(theme: Dict[str, str]) -> str:
    return """
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; }}
body {{
    background-color: {app_bg};
    background-image: {bg_image};
    background-size: cover;
    background-position: center top;
    background-repeat: no-repeat;
    background-attachment: fixed;
    color: {text};
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    font-size: 14.5px;
    line-height: 1.6;
    -webkit-font-smoothing: antialiased;
    padding: 18px 20px 96px 20px;
}}
::-webkit-scrollbar {{ width: 11px; height: 11px; }}
::-webkit-scrollbar-thumb {{ background: {border}; border-radius: 8px; border: 3px solid {app_bg}; }}
::-webkit-scrollbar-thumb:hover {{ background: {muted}; }}

.msg {{ display: flex; gap: 12px; margin: 0 0 22px 0; align-items: flex-start; animation: rise .25s ease both; }}
.msg.user {{ flex-direction: row-reverse; }}
@keyframes rise {{ from {{ opacity: 0; transform: translateY(8px); }} to {{ opacity: 1; transform: none; }} }}

.avatar {{
    flex: 0 0 auto; width: 34px; height: 34px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 11px; font-weight: 700; letter-spacing: .3px; color: #fff;
    box-shadow: 0 2px 6px {shadow}; user-select: none;
}}
.avatar.assistant {{ background: linear-gradient(135deg, #6d7cff, #8b5cf6); }}
.avatar.user {{ background: linear-gradient(135deg, {user_grad_a}, {user_grad_b}); }}
.avatar.system {{ background: linear-gradient(135deg, #64748b, #475569); }}
.avatar.error {{ background: linear-gradient(135deg, #ef4444, #b91c1c); }}

.col {{ display: flex; flex-direction: column; max-width: 78%; min-width: 0; }}
.msg.user .col {{ align-items: flex-end; }}

.meta {{ font-size: 11.5px; color: {muted}; margin: 1px 6px 5px 6px; display: flex; gap: 8px; align-items: center; }}
.meta .name {{ font-weight: 700; color: {text}; }}
.meta .cost {{ font-variant-numeric: tabular-nums; }}

.bubble {{
    background: {surface}; border: 1px solid {border}; border-radius: 16px;
    padding: 11px 15px; box-shadow: 0 1px 2px {shadow}; overflow-wrap: anywhere;
}}
.msg.assistant .bubble {{ border-top-left-radius: 5px; }}
.msg.user .bubble {{
    border-top-right-radius: 5px; border: none; color: {user_fg};
    background: linear-gradient(135deg, {user_grad_a}, {user_grad_b});
}}
.msg.user .bubble a {{ color: #eaf1ff; text-decoration: underline; }}
.msg.system .bubble {{ background: {surface_2}; color: {muted}; font-size: 13px; }}
.msg.error .bubble {{ background: {surface}; border-color: #ef4444; }}

.bubble p {{ margin: 0 0 10px 0; }}
.bubble p:last-child {{ margin-bottom: 0; }}
.bubble > :first-child {{ margin-top: 0; }}
.bubble > :last-child {{ margin-bottom: 0; }}

.bubble h1, .bubble h2, .bubble h3, .bubble h4, .bubble h5, .bubble h6 {{
    margin: 16px 0 8px 0; line-height: 1.3; font-weight: 700;
}}
.bubble h1 {{ font-size: 1.4em; }}
.bubble h2 {{ font-size: 1.25em; }}
.bubble h3 {{ font-size: 1.12em; }}
.bubble h4 {{ font-size: 1em; color: {muted}; text-transform: uppercase; letter-spacing: .4px; }}

.bubble ul, .bubble ol {{ margin: 8px 0; padding-left: 22px; }}
.bubble li {{ margin: 3px 0; }}
.bubble li::marker {{ color: {accent}; }}

.bubble a {{ color: {accent}; text-decoration: none; }}
.bubble a:hover {{ text-decoration: underline; }}

code.inline {{
    background: {code_bg}; color: {code_fg}; border-radius: 6px;
    padding: 1.5px 6px; font-size: .88em;
    font-family: "SF Mono", ui-monospace, "JetBrains Mono", Menlo, Consolas, monospace;
}}
.msg.user code.inline {{ background: rgba(255,255,255,.22); color: #fff; }}

.codeblock {{
    margin: 12px 0; border-radius: 12px; overflow: hidden;
    border: 1px solid {border}; box-shadow: 0 1px 2px {shadow};
}}
.codeblock-head {{
    background: {pre_head}; color: #aeb6c2; padding: 7px 14px;
    font-size: 11px; letter-spacing: .5px; text-transform: uppercase;
    display: flex; align-items: center; justify-content: space-between;
    font-family: "SF Mono", ui-monospace, Menlo, monospace;
}}
.codeblock pre {{
    margin: 0; padding: 13px 15px; background: {pre_bg}; color: {pre_fg};
    overflow-x: auto; font-size: 12.8px; line-height: 1.55;
}}
.codeblock code {{
    font-family: "SF Mono", ui-monospace, "JetBrains Mono", Menlo, Consolas, monospace;
    background: none; padding: 0; color: inherit;
}}

blockquote {{
    margin: 10px 0; padding: 6px 14px; border-left: 3px solid {accent};
    background: {quote_bg}; border-radius: 0 8px 8px 0; color: {muted};
}}
blockquote p {{ margin: 4px 0; }}

hr {{ border: none; border-top: 1px solid {border}; margin: 16px 0; }}

.md-table {{ border-collapse: collapse; margin: 12px 0; width: 100%; font-size: 13px; }}
.md-table th, .md-table td {{ border: 1px solid {border}; padding: 7px 11px; text-align: left; }}
.md-table th {{ background: {table_head}; font-weight: 700; }}
.msg.user .md-table th, .msg.user .md-table td {{ border-color: rgba(255,255,255,.35); }}

.typing .bubble {{ padding: 14px 18px; display: flex; align-items: center; gap: 12px; }}
.dots {{ display: inline-flex; gap: 5px; align-items: center; flex-shrink: 0; }}
.dots span {{
    width: 7px; height: 7px; border-radius: 50%; background: {muted};
    animation: blink 1.4s infinite both;
}}
.dots span:nth-child(2) {{ animation-delay: .2s; }}
.dots span:nth-child(3) {{ animation-delay: .4s; }}
@keyframes blink {{ 0%, 80%, 100% {{ opacity: .25; transform: scale(.8); }} 40% {{ opacity: 1; transform: scale(1); }} }}
.ai-status {{ font-size: .85em; color: {muted}; font-style: italic; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.stream-text {{ word-break: break-word; line-height: 1.6; font-size: .97em; }}
.layer-gallery {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:8px; }}
.layer-gallery figure {{ margin:0; text-align:center; }}
.layer-gallery img {{ display:block; width:160px; height:auto; border-radius:6px;
    border:1px solid {border}; cursor:zoom-in; transition:transform .15s; }}
.layer-gallery img:hover {{ transform:scale(1.04); }}
.layer-gallery figcaption {{ font-size:.75em; color:{muted}; margin-top:3px; }}

#lb-overlay {{
    display:none; position:fixed; top:0; left:0; right:0; bottom:0;
    background:rgba(0,0,0,.88); z-index:9999;
    align-items:center; justify-content:center; cursor:zoom-out;
}}
#lb-overlay img {{
    max-width:92vw; max-height:92vh; border-radius:10px;
    box-shadow:0 8px 48px rgba(0,0,0,.7); object-fit:contain;
}}

.actions {{ display: flex; gap: 4px; margin: 6px 4px 0 4px; }}
.msg.user .actions {{ justify-content: flex-end; }}
.act {{
    display: inline-flex; align-items: center; gap: 5px; cursor: pointer;
    font-size: 11.5px; color: {muted}; padding: 3px 9px; border-radius: 8px;
    text-decoration: none; border: 1px solid transparent; transition: all .12s ease;
}}
.act:hover {{ color: {text}; background: {surface}; border-color: {border}; }}
.act svg {{ width: 13px; height: 13px; }}

.nav-btn {{
    display: inline-flex; align-items: center; gap: 5px; cursor: pointer;
    font-size: .82em; font-weight: 600; padding: 3px 10px 3px 8px; border-radius: 8px;
    text-decoration: none; border: 1px solid; transition: all .12s ease;
    vertical-align: middle; margin: 1px 2px;
}}
.nav-pcb {{ color: {accent}; border-color: {accent}55; background: {accent}11; }}
.nav-pcb:hover {{ background: {accent}28; }}
.nav-sch {{ color: #a855f7; border-color: #a855f766; background: #a855f711; }}
.nav-sch:hover {{ background: #a855f724; }}
.nav-btn svg {{ width: 12px; height: 12px; flex-shrink: 0; }}

.empty {{
    text-align: center; color: {muted}; margin: 12vh auto 0 auto; max-width: 440px;
}}
.empty .logo {{
    width: 62px; height: 62px; border-radius: 18px; margin: 0 auto 18px auto;
    background: linear-gradient(135deg, #6d7cff, #8b5cf6);
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 8px 24px {shadow};
}}
.empty h2 {{ color: {text}; font-size: 20px; margin: 0 0 8px 0; }}
.empty p {{ margin: 0; font-size: 13.5px; line-height: 1.6; }}
.empty code {{ background: {code_bg}; color: {accent}; padding: 1px 6px; border-radius: 5px; font-size: .9em; }}
""".format(**theme)


_ICON_COPY = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
              'stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" '
              'height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>')
_ICON_EDIT = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
              'stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/>'
              '<path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>')
_ICON_LOGO = ('<svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="#fff" '
              'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
              '<path d="M12 2v3M12 19v3M2 12h3M19 12h3"/><rect x="7" y="7" width="10" height="10" rx="2"/>'
              '<circle cx="12" cy="12" r="1.6" fill="#fff"/></svg>')


def _message_html(index: int, role: str, content_html: str, meta_html: str,
                  actions: str, avatar_html: str = "", pre_bubble: str = "") -> str:
    av = avatar_html if avatar_html else _AVATAR.get(role, "AI")
    return (
        '<div class="msg {role}">'
        '<div class="avatar {role}">{avatar}</div>'
        '<div class="col">{meta}{pre_bubble}<div class="bubble">{body}</div>{actions}</div>'
        "</div>"
    ).format(role=role, avatar=av, meta=meta_html, pre_bubble=pre_bubble,
             body=content_html, actions=actions)


def _actions_html(index: int, role: str) -> str:
    items = ['<a class="act" href="app://copy?i={i}">{icon} Copy</a>'.format(i=index, icon=_ICON_COPY)]
    if role == "user":
        items.append('<a class="act" href="app://edit?i={i}">{icon} Edit</a>'.format(i=index, icon=_ICON_EDIT))
    return '<div class="actions">{}</div>'.format("".join(items))


def render_conversation(
    messages: List[Dict[str, object]],
    theme: Dict[str, str],
    *,
    typing: bool = False,
    empty_hint: str = "",
) -> str:
    """Build the full themed HTML document for the conversation."""
    body_parts: List[str] = []

    if not messages and not typing:
        body_parts.append(
            '<div class="empty"><div class="logo">{logo}</div>'
            "<h2>Ready</h2><p>{hint}</p></div>".format(
                logo=_ICON_LOGO, hint=html.escape(empty_hint, quote=False))
        )
    else:
        # Pre-build assistant avatar HTML once
        _av_src = theme.get("avatar_src", "")
        _ai_avatar = (
            '<img src="{src}" style="width:100%;height:100%;border-radius:50%;object-fit:cover;"/>'
            .format(src=_av_src)
        ) if _av_src else "AI"

        for index, item in enumerate(messages):
            role = str(item.get("role", "assistant"))
            if role not in ("user", "assistant", "system", "error"):
                role = "assistant"
            raw = str(item.get("content", ""))

            if role in ("user", "system"):
                content_html = "<p>{}</p>".format("<br/>".join(
                    _render_inline(line) if line.strip() else ""
                    for line in raw.split("\n")))
            elif role == "error":
                content_html = "<p>{}</p>".format("<br/>".join(
                    _render_inline(line) if line.strip() else ""
                    for line in raw.split("\n")))
            else:
                content_html = markdown_to_html(raw)

            name = str(item.get("name", "")) or {
                "user": "You", "assistant": "Maelectrix",
                "system": "System", "error": "API Error",
            }.get(role, "AI")

            cost = ""
            try:
                cost_val = float(item.get("prompt_cost_usd", 0.0) or 0.0)
                if cost_val > 0.0:
                    cost = '<span class="cost">· ${:.5f}</span>'.format(cost_val)
            except Exception:
                cost = ""

            meta_html = '<div class="meta"><span class="name">{name}</span>{cost}</div>'.format(
                name=html.escape(name, quote=False), cost=cost)

            actions = _actions_html(index, role) if role in ("user", "assistant") else ""
            avatar_html = _ai_avatar if role == "assistant" else ""

            # Build layer image gallery above the bubble
            pre_bubble = ""
            layer_images = item.get("layer_images") if role == "assistant" else None
            if layer_images:
                gallery_items = []
                for lname, lpath in list(layer_images.items())[:6]:
                    # Embed as base64 to bypass WKWebView file:// sandbox restrictions
                    src = ""
                    if lpath and os.path.exists(lpath):
                        try:
                            with open(lpath, "rb") as _fh:
                                _b64 = base64.b64encode(_fh.read()).decode("ascii")
                            mime = "image/svg+xml" if lpath.endswith(".svg") else "image/png"
                            src = "data:{};base64,{}".format(mime, _b64)
                        except Exception:
                            src = "file://{}".format(lpath.replace("\\", "/"))
                    if not src:
                        continue
                    escaped_label = html.escape(lname, quote=True)
                    gallery_items.append(
                        '<figure><img src="{src}" alt="{lbl}" title="{lbl}"/>'
                        "<figcaption>{lbl}</figcaption></figure>".format(
                            src=src, lbl=escaped_label)
                    )
                if gallery_items:
                    pre_bubble = '<div class="layer-gallery">{}</div>'.format(
                        "".join(gallery_items))

            body_parts.append(_message_html(index, role, content_html, meta_html, actions, avatar_html, pre_bubble))

    if typing:
        _av_src = theme.get("avatar_src", "")
        av_html = (
            '<img src="{src}" style="width:100%;height:100%;border-radius:50%;object-fit:cover;"/>'
            .format(src=_av_src)
        ) if _av_src else "AI"
        body_parts.append(
            '<div class="msg assistant typing"><div class="avatar assistant">{av}</div>'
            '<div class="col"><div class="meta"><span class="name">Maelectrix</span></div>'
            '<div class="bubble">'
            '<div id="thinking-area" style="display:flex;align-items:center;gap:12px;">'
            '<span class="dots"><span></span><span></span><span></span></span>'
            '<span id="ai-status" class="ai-status">Thinking…</span>'
            '</div>'
            '<div id="stream-content" class="stream-text" style="display:none"></div>'
            '</div></div></div>'.format(av=av_html)
        )

    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\"/>"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>"
        "<style>{css}</style></head><body>{body}"
        "<div id=\"anchor\"></div>"
        "<div id=\"lb-overlay\"></div>"
        "<script>"
        "document.addEventListener('click',function(e){{"
        "var img=e.target.closest('.layer-gallery img');"
        "if(img){{e.preventDefault();"
        "var lb=document.getElementById('lb-overlay');"
        "var li=lb.querySelector('img');if(!li){{li=document.createElement('img');lb.appendChild(li);}}"
        "li.src=img.src;li.alt=img.alt;"
        "lb.style.display='flex';}}"
        "}});"
        "document.getElementById('lb-overlay').addEventListener('click',function(){{"
        "this.style.display='none';}});"
        "document.addEventListener('keydown',function(e){{"
        "if(e.key==='Escape'){{var lb=document.getElementById('lb-overlay');"
        "if(lb)lb.style.display='none';}}}});"
        "</script>"
        "</body></html>"
    ).format(css=_css(theme), body="".join(body_parts))
