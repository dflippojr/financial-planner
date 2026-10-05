"""Structural accessibility checks for rendered HTML, without a browser dependency.

CSS visibility, contrast, and actual assistive-technology behavior need a browser
or manual audit. These checks deliberately cover the server-rendered structure.
"""

from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass(eq=False)
class Element:
    tag: str
    attrs: dict
    ancestors: tuple
    children: list = field(default_factory=list)

    @property
    def hidden(self):
        return any(
            "hidden" in node.attrs
            or node.attrs.get("aria-hidden") == "true"
            or "hidden" in node.attrs.get("class", "").split()
            or node.tag in {"script", "style", "template"}
            for node in (*self.ancestors, self)
        )

    @property
    def text(self):
        if self.hidden:
            return ""
        return " ".join(
            child.text if isinstance(child, Element) else child
            for child in self.children
        ).strip()


class PageParser(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        node = Element(tag, dict(attrs), tuple(self.stack))
        self.elements.append(node)
        if self.stack:
            self.stack[-1].children.append(node)
        if tag not in self.VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.stack:
            self.stack[-1].children.append(data)


def accessibility_errors(html):
    parser = PageParser()
    parser.feed(html)
    nodes = parser.elements
    errors = []
    ids = Counter(node.attrs["id"] for node in nodes if "id" in node.attrs)
    for value, count in ids.items():
        if count > 1:
            errors.append(f"duplicate id: {value}")
    if sum(node.tag == "h1" for node in nodes) != 1:
        errors.append("expected exactly one h1")
    by_id = {node.attrs["id"]: node for node in nodes if "id" in node.attrs}

    def aria_name(node):
        refs = node.attrs.get("aria-labelledby", "").split()
        if refs:
            return all(ref in by_id and by_id[ref].text for ref in refs)
        return bool(node.attrs.get("aria-label", "").strip())

    labels = [node for node in nodes if node.tag == "label" and (node.text or aria_name(node))]
    for node in nodes:
        description = f"<{node.tag}> {node.attrs.get('id') or node.attrs.get('name', '')}"
        if node.tag in {"input", "select", "textarea"} and not node.hidden:
            if node.tag == "input" and node.attrs.get("type", "text").lower() in {"hidden", "submit", "button"}:
                continue
            labelled = any(
                label in node.ancestors
                or (node.attrs.get("id") and label.attrs.get("for") == node.attrs["id"])
                for label in labels
            )
            if not aria_name(node) and not labelled:
                errors.append(f"control has no accessible name: {description}")
        if node.tag in {"button", "a"} and not node.hidden and not (node.text or aria_name(node)):
            errors.append(f"link/button has no accessible name: {description}")
        if node.tag == "img" and "alt" not in node.attrs:
            errors.append(f"image has no alt: {description}")
        if node.tag == "table" and not any(child.tag == "th" and node in child.ancestors for child in nodes):
            errors.append(f"table has no th: {description}")
        if node.tag == "th" and any(parent.tag == "thead" for parent in node.ancestors) and node.attrs.get("scope") != "col":
            errors.append(f"thead header needs scope=col: {description}")
    return errors


def assert_accessible(html):
    errors = accessibility_errors(html)
    assert not errors, "\n".join(errors)
