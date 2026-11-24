"""Text helpers: HTML to plain text, canonical URLs and de-duplication keys."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_BLOCK = {
    "p",
    "div",
    "br",
    "li",
    "ul",
    "ol",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "tr",
    "section",
    "article",
    "header",
    "footer",
    "table",
}
_SKIP = {"script", "style", "noscript"}
TRACKING_PARAMS = re.compile(
    r"^(utm_\w+|ref|referrer|source|src|gh_src|lever-source|fbclid|gclid)$"
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP:
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str | None) -> str:
    """Convert HTML (possibly entity-escaped twice, as Greenhouse does) to readable text."""
    if not raw:
        return ""
    if "&lt;" in raw and "<" not in raw:
        raw = html.unescape(raw)
    ex = _TextExtractor()
    ex.feed(raw)
    text = "".join(ex.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class _DivByClass(HTMLParser):
    """Collect the inner HTML of the first <div> whose class list contains `cls`."""

    def __init__(self, cls: str) -> None:
        super().__init__(convert_charrefs=False)
        self.cls = cls
        self.depth = 0
        self.out: list[str] = []
        self.done = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if self.done:
            return
        if self.depth:
            self.out.append(self.get_starttag_text() or "")
            if tag == "div":
                self.depth += 1
        elif tag == "div" and self.cls in (dict(attrs).get("class") or "").split():
            self.depth = 1

    def handle_endtag(self, tag: str) -> None:
        if not self.depth or self.done:
            return
        if tag == "div":
            self.depth -= 1
            if self.depth == 0:
                self.done = True
                return
        self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self.depth and not self.done:
            self.out.append(data)

    def handle_entityref(self, name: str) -> None:
        self.handle_data(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.handle_data(f"&#{name};")


def div_by_class(raw_html: str, cls: str) -> str:
    p = _DivByClass(cls)
    p.feed(raw_html)
    return "".join(p.out)


def canonical_url(url: str) -> str:
    """Lower-case scheme and host, drop fragments, tracking params and trailing slashes."""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if not TRACKING_PARAMS.match(k.lower())]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.lower() or "https", parts.netloc.lower(), path, urlencode(sorted(query)), "")
    )


def norm_words(text: str) -> str:
    text = html.unescape(text).lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_COMPANY_SUFFIX = re.compile(
    r"\b(inc|llc|ltd|limited|gmbh|corp|corporation|co|plc|bv|sa|ag|technologies|technology)\b"
)


def norm_company(name: str) -> str:
    return re.sub(r"\s+", " ", _COMPANY_SUFFIX.sub(" ", norm_words(name))).strip()


def dedupe_key(title: str, company: str) -> str:
    return f"{norm_words(title)}|{norm_company(company)}"
