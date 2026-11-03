"""Render an assistant chat reply as a small markdown subset, escaping first (#634).

Model output is untrusted: a reply can echo imported transaction descriptions. No HTML from the
reply reaches the page. The text is split into blocks and inline spans, every literal piece is
escaped as it is written, and markup comes only from the fixed tags this module writes:
paragraphs and line breaks, strong and em, inline code, fenced code blocks, headings (h3 to h6, so
a reply never adds a page-level heading), one level of nested lists, pipe tables, block quotes and
rules. A link is kept only for a same-origin path that starts with a single "/"; anything else
stays as text, and bare URLs are never linked.

Saved turns (the chat_markdown_html template filter) and live turns (the chat-turn JSON) both call
render(), so the two paths produce the same markup.
"""

import html
import re

from django.utils.safestring import mark_safe

# Block quotes nest at most this deep; a deeper ">" is shown as text, which also bounds recursion.
MAX_QUOTE_DEPTH = 3
# Emphasis nests at most this deep; anything inside the last level is shown as text.
MAX_INLINE_DEPTH = 6

# Inline matches are bounded so each start position does a fixed amount of work (no ReDoS). An
# emphasis body steps over single-backtick code spans whole, so a "**" inside code never closes it.
_BODY = r"(?:`[^`]{1,200}`|[^`]){1,1000}?"
_INLINE = re.compile(
    r"\\(?P<esc>[!-/:-@\[-`{-~])"
    r"|(?<!`)(?P<tick>`+)(?!`)(?P<code>.{1,1000}?)(?<!`)(?P=tick)(?!`)"
    r"|\[(?P<label>[^\[\]]{1,500})\]\((?P<url>[^\s()]{1,2048})\)"
    r"|\*\*\*(?P<both>(?=\S)" + _BODY + r"(?<=\S))\*\*\*(?!\*)"
    r"|\*\*(?P<strong>(?=\S)" + _BODY + r"(?<=\S))\*\*(?!\*)"
    r"|(?<!\w)__(?P<strong2>(?=\S)" + _BODY + r"(?<=\S))__(?!\w)"
    r"|(?<!\*)\*(?P<em>(?=[^\s*])" + _BODY + r"(?:(?<=[^\s*])|(?<=\*\*)))\*(?!\*)"
    r"|(?<!\w)_(?P<em2>(?=[^\s_])" + _BODY + r"(?<=[^\s_]))_(?!\w)"
)

_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
_HEADING = re.compile(r" {0,3}(#{1,6})[ \t]+(.*)")
_QUOTE = re.compile(r" {0,3}> ?(.*)")
_LIST_ITEM = re.compile(r"( *)([-*+]|\d{1,9}[.)])[ \t]+(.*)")
_DELIMITER_CELL = re.compile(r":?-+:?")
_URL_REFUSED = frozenset("\\\"'<>`")


def render(text):
    """The reply as safe HTML: one div.chat-md holding only markup built from the subset."""
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return mark_safe('<div class="chat-md">' + "".join(_blocks(lines, 0)) + "</div>")  # noqa: S308 - every literal is escaped by _text; tags come only from this module


def safe_href(url):
    """True only for a same-origin path: a single leading "/", no backslash, space or control char.

    Browsers drop tabs and newlines from URLs and read "\\" as "/", so "/\\evil.example" and
    "/<tab>/evil.example" would both leave the site; any such character rejects the link. Quotes,
    angle brackets and backticks are refused too, although the href is escaped either way.
    """
    if not url.startswith("/") or url.startswith("//"):
        return False
    return not any(
        char in _URL_REFUSED or char.isspace() or ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in url
    )


def _text(value):
    return html.escape(value, quote=True)


def _inline(text, links=True, depth=0):
    if depth >= MAX_INLINE_DEPTH:
        return _text(text)
    out = []
    pos = 0
    while True:
        match = _INLINE.search(text, pos)
        if match is None:
            out.append(_text(text[pos:]))
            return "".join(out)
        out.append(_text(text[pos:match.start()]))
        out.append(_span(match, links, depth + 1))
        pos = match.end()


def _span(match, links, depth):
    kind = match.lastgroup
    if kind == "esc":
        return _text(match["esc"])
    if match["tick"] is not None:
        code = match["code"]
        if len(code) > 2 and code.startswith(" ") and code.endswith(" ") and code.strip():
            code = code[1:-1]
        return "<code>" + _text(code) + "</code>"
    if match["label"] is not None:
        if not links or not safe_href(match["url"]):
            return _text(match.group(0))
        return '<a href="' + _text(match["url"]) + '">' + _inline(match["label"], False, depth) + "</a>"
    if match["both"] is not None:
        return "<em><strong>" + _inline(match["both"], links, depth) + "</strong></em>"
    strong = match["strong"] if match["strong"] is not None else match["strong2"]
    if strong is not None:
        return "<strong>" + _inline(strong, links, depth) + "</strong>"
    em = match["em"] if match["em"] is not None else match["em2"]
    return "<em>" + _inline(em, links, depth) + "</em>"


def _lines_html(lines):
    return "<br>".join(_inline(line.strip()) for line in lines)


def _is_blank(line):
    return not line.strip()


def _is_rule(line):
    if len(line) - len(line.lstrip(" ")) > 3:
        return False
    marks = line.replace(" ", "").replace("\t", "")
    return len(marks) >= 3 and len(set(marks)) == 1 and marks[0] in "-*_"


def _heading(line):
    match = _HEADING.fullmatch(line)
    if match is None:
        return None
    level = min(len(match[1]) + 2, 6)
    content = match[2].rstrip()
    closed = content.rstrip("#")
    if not closed:
        content = ""
    elif closed != content and closed[-1] in " \t":
        content = closed.rstrip()
    return f"<h{level}>{_inline(content)}</h{level}>"


def _cells(line):
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", row)]


def _table_starts(lines, i):
    if i + 1 >= len(lines) or "|" not in lines[i] or "|" not in lines[i + 1]:
        return False
    header, delimiter = _cells(lines[i]), _cells(lines[i + 1])
    return len(header) == len(delimiter) and all(_DELIMITER_CELL.fullmatch(cell) for cell in delimiter)


def _starts_block(lines, i, depth):
    line = lines[i]
    return bool(
        _FENCE.match(line)
        or _HEADING.fullmatch(line)
        or _is_rule(line)
        or (depth < MAX_QUOTE_DEPTH and _QUOTE.match(line))
        or _LIST_ITEM.fullmatch(line)
        or _table_starts(lines, i)
    )


def _blocks(lines, depth):
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _is_blank(line):
            i += 1
        elif fence := _FENCE.match(line):
            i = _code_block(lines, i, fence[1], out)
        elif heading := _heading(line):
            out.append(heading)
            i += 1
        elif _is_rule(line):
            out.append("<hr>")
            i += 1
        elif depth < MAX_QUOTE_DEPTH and _QUOTE.match(line):
            i = _quote(lines, i, depth, out)
        elif item := _LIST_ITEM.fullmatch(line):
            i = _list(lines, i, item, depth, out)
        elif _table_starts(lines, i):
            i = _table(lines, i, out)
        else:
            i = _paragraph(lines, i, depth, out)
    return out


def _code_block(lines, i, fence, out):
    body = []
    i += 1
    while i < len(lines):
        closing = lines[i].strip()
        if closing.startswith(fence) and set(closing) == {fence[0]} and len(lines[i]) - len(lines[i].lstrip(" ")) <= 3:
            i += 1
            break
        body.append(lines[i])
        i += 1
    out.append("<pre><code>" + _text("\n".join(body)) + "</code></pre>")
    return i


def _quote(lines, i, depth, out):
    inner = []
    while i < len(lines) and (match := _QUOTE.match(lines[i])):
        inner.append(match[1])
        i += 1
    out.append("<blockquote>" + "".join(_blocks(inner, depth + 1)) + "</blockquote>")
    return i


def _paragraph(lines, i, depth, out):
    start = i
    i += 1
    while i < len(lines) and not _is_blank(lines[i]) and not _starts_block(lines, i, depth):
        i += 1
    out.append("<p>" + _lines_html(lines[start:i]) + "</p>")
    return i


class _Item:
    def __init__(self, text):
        self.lines = [text]
        self.children = []
        self.children_ordered = False


def _ordered(marker):
    return marker[0].isdigit()


class _List:
    """One list with at most one nested level; deeper items join the nested level."""

    def __init__(self, first):
        self.base = len(first[1])
        self.ordered = _ordered(first[2])
        self.start = int(first[2][:-1]) if self.ordered else 1
        self.items = []

    def take(self, line, after_blank, starts_block):
        """Add line to the list, or return False when the line ends it."""
        item = _LIST_ITEM.fullmatch(line)
        indent = len(line) - len(line.lstrip(" "))
        if item and indent < self.base + 2:
            if _ordered(item[2]) != self.ordered:
                return False
            self.items.append(_Item(item[3]))
            return True
        last = self.items[-1]
        if item:
            if not last.children:
                last.children_ordered = _ordered(item[2])
            last.children.append(_Item(item[3]))
            return True
        if indent >= self.base + 2 or not (after_blank or starts_block):
            (last.children[-1] if last.children else last).lines.append(line)
            return True
        return False

    def html(self):
        tag = "ol" if self.ordered else "ul"
        parts = [f'<ol start="{self.start}">' if self.ordered and self.start != 1 else f"<{tag}>"]
        for item in self.items:
            parts.append("<li>" + _lines_html(item.lines))
            if item.children:
                child_tag = "ol" if item.children_ordered else "ul"
                parts.append(f"<{child_tag}>")
                parts.extend("<li>" + _lines_html(child.lines) + "</li>" for child in item.children)
                parts.append(f"</{child_tag}>")
            parts.append("</li>")
        parts.append(f"</{tag}>")
        return "".join(parts)


def _list(lines, i, first, depth, out):
    builder = _List(first)
    blank = False
    while i < len(lines):
        if _is_blank(lines[i]):
            blank = True
        elif builder.take(lines[i], blank, _starts_block(lines, i, depth)):
            blank = False
        else:
            break
        i += 1
    out.append(builder.html())
    return i


def _align(cell):
    if cell.startswith(":") and cell.endswith(":"):
        return ' class="chat-md-center"'
    if cell.endswith(":"):
        return ' class="chat-md-right"'
    return ""


def _table(lines, i, out):
    header = _cells(lines[i])
    aligns = [_align(cell) for cell in _cells(lines[i + 1])]
    width = len(header)
    i += 2
    rows = []
    while i < len(lines) and not _is_blank(lines[i]) and "|" in lines[i]:
        cells = _cells(lines[i])[:width]
        rows.append(cells + [""] * (width - len(cells)))
        i += 1
    parts = ['<div class="chat-md-table" tabindex="0" role="region" aria-label="Table"><table><thead><tr>']
    parts.extend(f'<th scope="col"{aligns[n]}>{_inline(cell)}</th>' for n, cell in enumerate(header))
    parts.append("</tr></thead>")
    if rows:
        parts.append("<tbody>")
        for row in rows:
            parts.append("<tr>" + "".join(f"<td{aligns[n]}>{_inline(cell)}</td>" for n, cell in enumerate(row)) + "</tr>")
        parts.append("</tbody>")
    parts.append("</table></div>")
    out.append("".join(parts))
    return i
