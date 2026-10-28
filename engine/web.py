"""URL to elements. The fetch refuses private and internal addresses, because the API fetches for strangers."""
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from . import confidence as conf
from . import mdparse
from .schema import Element

HEADERS = {"User-Agent": "Mozilla/5.0 (extraction-service)"}
MAX_BYTES = 40 * 1024 * 1024


class UnsafeURL(ValueError):
    pass


def check_url(url):
    """Only http(s) URLs whose host resolves to public addresses are allowed (no metadata or internal hosts)."""
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeURL("Only http and https links are allowed.")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror:
        raise UnsafeURL("The host name could not be resolved.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise UnsafeURL("That address is not public.")


def fetch(url, timeout=30):
    """Fetch with redirects checked one by one. Returns (bytes, content type)."""
    for _ in range(5):
        check_url(url)
        response = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=False, stream=True)
        if response.is_redirect or response.status_code in (301, 302, 303, 307, 308):
            url = urljoin(url, response.headers.get("Location", ""))
            continue
        response.raise_for_status()
        body = b""
        for chunk in response.iter_content(65536):
            body += chunk
            if len(body) > MAX_BYTES:
                raise UnsafeURL("The file is larger than 40 MB.")
        return body, response.headers.get("Content-Type", ""), url
    raise UnsafeURL("Too many redirects.")


def is_pdf(body, content_type, url):
    return "pdf" in content_type.lower() or urlparse(url).path.lower().endswith(".pdf") or body[:5] == b"%PDF-"


def html_elements(html, base_url):
    """Headings, paragraphs, lists, tables, formulas and images in document order. HTML text is exact, so
    its confidence is high; tables are scored by how regular they are."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "aside", "form", "noscript"]):
        if tag.name == "script" and "math/tex" in (tag.get("type") or ""):
            continue
        tag.decompose()
    root = soup.find(id="mw-content-text") or soup.find("article") or soup.find("main") or soup.body or soup
    elements = []
    seen = set()

    def inside(tag, names):
        return any(p.name in names for p in tag.parents)

    for tag in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "table", "img", "dl", "math", "script", "pre"]):
        if id(tag) in seen:
            continue
        if tag.name == "script":
            if "math/tex" not in (tag.get("type") or ""):
                continue
            elements.append(Element("formula", 1, tag.get_text().strip(), 0.9, "html", data={"display": "mode=display" in (tag.get("type") or "")}))
            continue
        if tag.name == "math":
            if inside(tag, ("table",)):
                continue
            ann = tag.find("annotation", attrs={"encoding": re.compile("tex", re.I)})
            if ann and ann.get_text().strip():
                elements.append(Element("formula", 1, ann.get_text().strip(), 0.9, "html"))
            else:
                text = tag.get_text(" ", strip=True)
                if text:
                    elements.append(Element("formula", 1, text, 0.4, "html", flags=["formula_not_rebuilt"]))
            for d in tag.find_all(True):
                seen.add(id(d))
            continue
        if tag.name == "table":
            if inside(tag, ("table",)):
                continue
            rows = []
            for tr in tag.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                if cells:
                    rows.append(cells)
            rows = [r for r in rows if any(r)]
            if len(rows) >= 2 and max(len(r) for r in rows) >= 2:
                caption = tag.find("caption")
                el = Element("table", 1, mdparse.table_markdown(rows), conf.table_confidence(rows), "html", data={"rows": rows})
                if caption and caption.get_text(strip=True):
                    elements.append(Element("text", 1, caption.get_text(" ", strip=True), 0.95, "html"))
                elements.append(el)
            for d in tag.find_all(True):
                seen.add(id(d))
            continue
        if inside(tag, ("table", "nav", "footer")):
            continue
        if tag.name == "img":
            src = tag.get("src") or tag.get("data-src")
            if not src or src.startswith("data:"):
                continue
            url = urljoin(base_url, src)
            name = (tag.get("alt") or url.rsplit("/", 1)[-1] or "image").strip()
            elements.append(Element("image", 1, f"![{name}]({url})", 0.9, "html", data={"name": name, "url": url}))
            continue
        if tag.name == "dl":
            for dt in tag.find_all("dt"):
                dd = dt.find_next_sibling("dd")
                if dd:
                    key, value = dt.get_text(" ", strip=True), dd.get_text(" ", strip=True)
                    elements.append(Element("key_value", 1, f"{key}: {value}", 0.95, "html",
                                            data={"key": key, "value": value}, in_markdown=False))
            for d in tag.find_all(True):
                seen.add(id(d))
            continue
        text = tag.get_text(" ", strip=True)
        if not text:
            continue
        if tag.name.startswith("h") and len(tag.name) == 2:
            elements.append(Element("heading", 1, text, 0.97, "html", data={"level": min(int(tag.name[1]), 4)}))
        elif tag.name == "li":
            elements.append(Element("text", 1, "- " + text, 0.95, "html"))
            for d in tag.find_all(["p", "li"]):
                seen.add(id(d))
        elif tag.name == "pre":
            elements.append(Element("text", 1, "```\n" + tag.get_text() + "\n```", 0.95, "html"))
        else:
            elements.append(Element("text", 1, text, 0.95, "html"))
    return mdparse.add_key_values(elements)