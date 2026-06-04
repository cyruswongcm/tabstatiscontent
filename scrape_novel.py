#!/usr/bin/env python3
"""
General-purpose scraper for zhenhunxiaoshuo.com novels.

Usage:
    python3 scrape_novel.py <book-index-url> [output.pdf]

Examples:
    python3 scrape_novel.py https://www.zhenhunxiaoshuo.com/taifengyan/
    python3 scrape_novel.py https://www.zhenhunxiaoshuo.com/saye/ 撒野_全文.pdf

The script:
  1. Fetches the book's index page and discovers all chapter links.
  2. Fetches each chapter, caching it as chapters_cache/<slug>/<id>.txt
  3. Compiles everything into a single CJK PDF.
     Font priority: PingFang (macOS) → WenQuanYi Zen Hei (Linux)
"""

import os
import sys
import re
import time
import unicodedata
import requests
from bs4 import BeautifulSoup
from pathlib import Path
from urllib.parse import urljoin, urlparse

# ── Constants ─────────────────────────────────────────────────────────────────

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

REQUEST_DELAY = 1.0       # polite delay between fetches (seconds)
MAX_RETRIES   = 4
RETRY_BACKOFF = [2, 4, 8, 16]

CACHE_ROOT = Path("chapters_cache")

# ── Font resolution ────────────────────────────────────────────────────────────

def find_cjk_font() -> str:
    candidates = [
        # macOS
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/Library/Fonts/Arial Unicode MS.ttf",
        # Linux
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJKsc-Regular.otf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    raise RuntimeError(
        "No CJK font found. On macOS PingFang is built-in; "
        "on Linux: sudo apt install fonts-wqy-zenhei"
    )

# ── HTTP helpers ──────────────────────────────────────────────────────────────

def get(session: requests.Session, url: str) -> requests.Response:
    headers = {**BASE_HEADERS, "Referer": url}
    for attempt, wait in enumerate(RETRY_BACKOFF, 1):
        try:
            r = session.get(url, headers=headers, timeout=20)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r
        except requests.HTTPError as e:
            print(f"    HTTP {e.response.status_code} (attempt {attempt})")
        except requests.RequestException as e:
            print(f"    {e} (attempt {attempt})")
        if attempt < MAX_RETRIES:
            time.sleep(wait)
    raise RuntimeError(f"Failed after {MAX_RETRIES} attempts: {url}")

# ── Index page parsing ────────────────────────────────────────────────────────

def discover_chapters(session: requests.Session, index_url: str) -> list[tuple[str, str]]:
    """
    Fetch the book's index page and return an ordered list of (title, url) for each chapter.
    Handles both root-level /{id}.html and nested /{slug}/{id}/ URL patterns.
    """
    print(f"[*] Fetching index: {index_url}")
    r = get(session, index_url)
    soup = BeautifulSoup(r.text, "lxml")

    index_parsed = urlparse(index_url)
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True)
        if not text or not href:
            continue

        full_url = urljoin(index_url, href)
        parsed   = urlparse(full_url)

        # Same domain only
        if parsed.netloc != index_parsed.netloc:
            continue

        path = parsed.path

        # Skip the index page itself
        if path.rstrip("/") == index_parsed.path.rstrip("/"):
            continue

        # Chapter URLs contain a run of 4+ digits after a slash:
        #   /132155.html  /taifengyan/132155.html  /read/177051/
        # Navigation slugs like /chunai2/ only have 1 digit — excluded.
        if not re.search(r"/\d{4,}", path):
            continue

        if full_url not in seen:
            seen.add(full_url)
            unique.append((text, full_url))

    if not unique:
        raise RuntimeError(
            f"No chapter links found on {index_url}.\n"
            "The site may be blocking this IP — run locally."
        )

    print(f"[*] Found {len(unique)} chapters")
    return unique

# ── Chapter fetching ──────────────────────────────────────────────────────────

def parse_chapter(html: str, fallback_title: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "lxml")

    title = None
    for sel in ["h1.title", "h1", ".chapter-title", ".title", "#chapter-title"]:
        tag = soup.select_one(sel)
        if tag and tag.get_text(strip=True):
            title = tag.get_text(strip=True)
            break
    if not title:
        t = soup.find("title")
        title = t.get_text(strip=True) if t else fallback_title

    body = None
    for sel in [
        "#chapter-content", "#content", ".chapter-content",
        ".content", "#article", ".read-content", "article",
    ]:
        tag = soup.select_one(sel)
        if tag:
            for noise in tag.find_all(["script", "style", "ins", "a"]):
                noise.decompose()
            body = tag.get_text("\n", strip=True)
            if len(body) > 100:
                break

    if not body:
        divs  = soup.find_all("div")
        body  = max((d.get_text("\n", strip=True) for d in divs), key=len, default="")

    return title, body


def url_to_cache_key(url: str) -> str:
    """Turn a chapter URL into a filesystem-safe filename (no extension)."""
    path = urlparse(url).path.strip("/")
    safe = re.sub(r"[^\w\-]", "_", path)
    return safe or url[-40:]


def load_or_fetch(
    session: requests.Session,
    cache_dir: Path,
    chapter_url: str,
    fallback_title: str,
) -> tuple[str, str]:
    key        = url_to_cache_key(chapter_url)
    cache_file = cache_dir / f"{key}.txt"

    if cache_file.exists():
        raw   = cache_file.read_text(encoding="utf-8")
        parts = raw.split("\n", 1)
        return parts[0].lstrip("# ").strip(), (parts[1].strip() if len(parts) > 1 else "")

    r          = get(session, chapter_url)
    title, body = parse_chapter(r.text, fallback_title)
    cache_file.write_text(f"# {title}\n{body}", encoding="utf-8")
    time.sleep(REQUEST_DELAY)
    return title, body

# ── PDF builder ───────────────────────────────────────────────────────────────

def build_pdf(
    book_title: str,
    chapters: list[tuple[str, str]],
    font_path: str,
    out_path: str,
):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, PageBreak, HRFlowable
    )
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    pdfmetrics.registerFont(TTFont("CJK", font_path, subfontIndex=0))
    print(f"[PDF] Font: {os.path.basename(font_path)}")

    doc = SimpleDocTemplate(
        out_path,
        pagesize=A4,
        leftMargin=2.5 * cm, rightMargin=2.5 * cm,
        topMargin=2.5 * cm,  bottomMargin=2.5 * cm,
        title=book_title,
    )

    cover_style = ParagraphStyle("Cover", fontName="CJK", fontSize=28, leading=38,
                                 alignment=TA_CENTER, spaceAfter=12)
    sub_style   = ParagraphStyle("Sub",   fontName="CJK", fontSize=12, alignment=TA_CENTER)
    head_style  = ParagraphStyle("Head",  fontName="CJK", fontSize=14, leading=20,
                                 spaceBefore=14, spaceAfter=10, alignment=TA_CENTER)
    body_style  = ParagraphStyle("Body",  fontName="CJK", fontSize=11, leading=18,
                                 alignment=TA_JUSTIFY, firstLineIndent=22)

    story = []

    # Cover
    story.append(Spacer(1, 6 * cm))
    story.append(Paragraph(book_title, cover_style))
    story.append(Spacer(1, 0.5 * cm))
    story.append(Paragraph(f"共 {len(chapters)} 章", sub_style))
    story.append(PageBreak())

    for idx, (title, body) in enumerate(chapters, 1):
        story.append(Paragraph(title, head_style))
        story.append(HRFlowable(width="100%", thickness=0.5, color="grey"))
        story.append(Spacer(1, 0.3 * cm))

        for para in re.split(r"\n{2,}", body):
            para = para.strip()
            if not para:
                continue
            para = para.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            for line in para.split("\n"):
                line = line.strip()
                if line:
                    story.append(Paragraph(line, body_style))

        if idx < len(chapters):
            story.append(PageBreak())

    doc.build(story)
    size_mb = os.path.getsize(out_path) / 1_048_576
    print(f"[PDF] Written → {out_path}  ({size_mb:.1f} MB)")

# ── Main ──────────────────────────────────────────────────────────────────────

def slug_from_url(url: str) -> str:
    return urlparse(url).path.strip("/").replace("/", "_") or "novel"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    index_url = sys.argv[1].rstrip("/") + "/"

    # Derive default output filename from URL slug if not provided
    slug = urlparse(index_url).path.strip("/")
    default_pdf = f"{slug}_全文.pdf"
    out_pdf = sys.argv[2] if len(sys.argv) >= 3 else default_pdf

    CACHE_ROOT.mkdir(exist_ok=True)
    cache_dir = CACHE_ROOT / slug_from_url(index_url)
    cache_dir.mkdir(exist_ok=True)

    font_path = find_cjk_font()
    session   = requests.Session()

    chapter_links = discover_chapters(session, index_url)

    total    = len(chapter_links)
    chapters = []
    errors   = []

    print(f"\n[*] Fetching {total} chapters → cache: {cache_dir}/\n")
    for i, (link_title, chapter_url) in enumerate(chapter_links, 1):
        try:
            title, body = load_or_fetch(session, cache_dir, chapter_url, link_title)
            chapters.append((title, body))
            cached = "cache" if (cache_dir / f"{url_to_cache_key(chapter_url)}.txt").exists() else "fetched"
            print(f"  [{i:3}/{total}] {title[:44]}  ({cached})")
        except Exception as e:
            print(f"  [{i:3}/{total}] ERROR {chapter_url}: {e}")
            errors.append((chapter_url, str(e)))

    if not chapters:
        print("\n[!] No chapters fetched. Exiting.")
        sys.exit(1)

    print(f"\n[*] {len(chapters)}/{total} chapters fetched"
          + (f"  ({len(errors)} errors)" if errors else ""))
    if errors:
        for url, err in errors:
            print(f"    {url}: {err}")

    # Use the book's slug as the title if we can't read the index page title
    book_title = slug
    print(f"\n[*] Building PDF: {out_pdf}")
    build_pdf(book_title, chapters, font_path, out_pdf)
    print(f"[✓] Done!  {out_pdf}  ({len(chapters)} chapters)")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(f"\n[!] {e}", file=sys.stderr)
        sys.exit(1)
