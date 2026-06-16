#!/usr/bin/env python3
"""
Scraper for 《人鱼陷落》 from zhenhunxiaoshuo.com
Chapters: IDs 177051–177328, skip 177311 (277 chapters total)
Output: 人鱼陷落_全文.pdf  (CJK font: PingFang → WenQuanYi fallback)
Cache:  chapters_cache/{id}.txt
"""

import os
import sys
import time
import re
import requests
from bs4 import BeautifulSoup
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────

BOOK_TITLE  = "人鱼陷落"
OUTPUT_PDF  = "人鱼陷落_全文.pdf"
CACHE_DIR   = Path("chapters_cache")

CHAPTER_IDS = [i for i in range(177051, 177329) if i != 177311]  # 277 chapters

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",

    "Referer": "https://www.zhenhunxiaoshuo.com/",
    "Connection": "keep-alive",
}

# URL patterns tried in order; first successful one is reused for all chapters
URL_PATTERNS = [
    "https://www.zhenhunxiaoshuo.com/read/{id}/",
    "https://www.zhenhunxiaoshuo.com/{id}.html",
    "https://www.zhenhunxiaoshuo.com/chapter/{id}.html",
    "https://www.zhenhunxiaoshuo.com/book/{id}.html",
]

REQUEST_DELAY   = 1.0   # seconds between requests (polite scraping)
MAX_RETRIES     = 4
RETRY_BACKOFF   = [2, 4, 8, 16]  # seconds

# ── Font resolution ────────────────────────────────────────────────────────────

def find_cjk_font() -> str:
    """Return path to the best available CJK TrueType font."""
    candidates = [
        # macOS PingFang (user's preferred font)
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/Library/Fonts/Arial Unicode MS.ttf",
        # Linux
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJKsc-Regular.otf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    raise RuntimeError(
        "No CJK font found. On macOS PingFang is built-in; "
        "on Linux install fonts-wqy-zenhei or fonts-noto-cjk."
    )

# ── Scraping ──────────────────────────────────────────────────────────────────

def probe_url_pattern(session: requests.Session, chapter_id: int) -> str:
    """Try each URL pattern and return the first that returns HTTP 200."""
    for pattern in URL_PATTERNS:
        url = pattern.format(id=chapter_id)
        try:
            r = session.get(url, headers=BASE_HEADERS, timeout=15)
            if r.status_code == 200:
                print(f"  [probe] working URL pattern: {pattern}")
                return pattern
            print(f"  [probe] {url} → {r.status_code}")
        except requests.RequestException as e:
            print(f"  [probe] {url} → {e}")
    raise RuntimeError(
        f"All URL patterns returned non-200 for chapter {chapter_id}.\n"
        "The site may be blocking datacenter IPs. Run this script on a local machine."
    )


def parse_chapter(html: str, chapter_id: int) -> tuple[str, str]:
    """
    Parse chapter HTML; return (title, body_text).
    Tries common CSS selectors used by Chinese novel sites.
    """
    soup = BeautifulSoup(html, "lxml")

    # Strip comments, share buttons, and sidebar noise before extraction
    for sel in ["#postcomments", ".postcomments", "#respond", ".comments-respond",
                ".comt", ".article-actions", ".shares", ".sidebar", ".article-meta"]:
        for tag in soup.select(sel):
            tag.decompose()
    for tag in soup.find_all("div", id=lambda x: x and x.startswith("div-comment-")):
        tag.decompose()

    # ── Title ──
    title = None
    for sel in ["h1.title", "h1", ".chapter-title", ".title", "#chapter-title"]:
        tag = soup.select_one(sel)
        if tag and tag.get_text(strip=True):
            title = tag.get_text(strip=True)
            break
    if not title:
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else f"第{chapter_id}章"

    # ── Body ──
    # article.article-content is this site's exact content container
    body = None
    for sel in [
        "article.article-content", ".article-content",
        "#chapter-content", "#content", ".chapter-content",
        ".content", "#article", ".read-content", "article",
    ]:
        tag = soup.select_one(sel)
        if tag:
            for noise in tag.find_all(["script", "style", "ins", "a"]):
                noise.decompose()
            body = tag.get_text("\n", strip=True)
            if len(body) > 50:
                break

    if not body:
        # Fallback: largest <div> by text length
        divs = soup.find_all("div")
        body = max((d.get_text("\n", strip=True) for d in divs), key=len, default="")

    return title, body


def fetch_chapter(
    session: requests.Session, url_pattern: str, chapter_id: int
) -> tuple[str, str]:
    """Fetch one chapter with retry/backoff; return (title, body)."""
    url = url_pattern.format(id=chapter_id)
    for attempt, wait in enumerate(RETRY_BACKOFF, 1):
        try:
            r = session.get(url, headers=BASE_HEADERS, timeout=20)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return parse_chapter(r.text, chapter_id)
        except requests.HTTPError as e:
            print(f"    HTTP {e.response.status_code} on attempt {attempt}")
        except requests.RequestException as e:
            print(f"    Network error on attempt {attempt}: {e}")
        if attempt < MAX_RETRIES:
            print(f"    Retrying in {wait}s…")
            time.sleep(wait)
    raise RuntimeError(f"Failed to fetch chapter {chapter_id} after {MAX_RETRIES} attempts")


def load_or_fetch(
    session: requests.Session, url_pattern: str, chapter_id: int
) -> tuple[str, str]:
    """Return (title, body) from cache or by fetching."""
    cache_file = CACHE_DIR / f"{chapter_id}.txt"
    if cache_file.exists():
        raw = cache_file.read_text(encoding="utf-8")
        lines = raw.split("\n", 1)
        title = lines[0].lstrip("# ").strip()
        body  = lines[1].strip() if len(lines) > 1 else ""
        if body:  # skip cache if body was empty (e.g. from a garbled prior run)
            return title, body
        print(f"    (cache empty, re-fetching {chapter_id})")

    title, body = fetch_chapter(session, url_pattern, chapter_id)

    cache_file.write_text(f"# {title}\n{body}", encoding="utf-8")
    time.sleep(REQUEST_DELAY)
    return title, body

# ── PDF generation ────────────────────────────────────────────────────────────

def build_pdf(chapters: list[tuple[str, str, int]], font_path: str, out_path: str):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, PageBreak, HRFlowable
    )
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    # Register CJK font
    font_name = "CJKFont"
    # .ttc files may contain multiple fonts; index 0 is usually the regular weight
    pdfmetrics.registerFont(TTFont(font_name, font_path, subfontIndex=0))

    font_label = os.path.basename(font_path)
    print(f"\n[PDF] Using font: {font_label}")

    doc = SimpleDocTemplate(
        out_path,
        pagesize=A4,
        leftMargin=2.5 * cm,
        rightMargin=2.5 * cm,
        topMargin=2.5 * cm,
        bottomMargin=2.5 * cm,
        title=BOOK_TITLE,
        author="zhenhunxiaoshuo.com",
    )

    styles = getSampleStyleSheet()

    cover_style = ParagraphStyle(
        "Cover",
        fontName=font_name,
        fontSize=28,
        leading=38,
        alignment=TA_CENTER,
        spaceAfter=12,
    )
    chapter_title_style = ParagraphStyle(
        "ChapterTitle",
        fontName=font_name,
        fontSize=14,
        leading=20,
        spaceBefore=14,
        spaceAfter=10,
        alignment=TA_CENTER,
    )
    body_style = ParagraphStyle(
        "Body",
        fontName=font_name,
        fontSize=11,
        leading=18,
        alignment=TA_JUSTIFY,
        firstLineIndent=22,
    )

    story = []

    # Cover page
    story.append(Spacer(1, 6 * cm))
    story.append(Paragraph(BOOK_TITLE, cover_style))
    story.append(Spacer(1, 0.5 * cm))
    story.append(Paragraph(f"共 {len(chapters)} 章", ParagraphStyle(
        "Sub", fontName=font_name, fontSize=12, alignment=TA_CENTER
    )))
    story.append(PageBreak())

    for idx, (title, body, chapter_id) in enumerate(chapters, 1):
        # Chapter heading
        story.append(Paragraph(title, chapter_title_style))
        story.append(HRFlowable(width="100%", thickness=0.5, color="grey"))
        story.append(Spacer(1, 0.3 * cm))

        # Body paragraphs (split on blank lines / newlines)
        for para in re.split(r"\n{2,}", body):
            para = para.strip()
            if not para:
                continue
            # Escape XML special chars for reportlab
            para = para.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            for line in para.split("\n"):
                line = line.strip()
                if line:
                    story.append(Paragraph(line, body_style))

        if idx < len(chapters):
            story.append(PageBreak())

    doc.build(story)
    print(f"[PDF] Written → {out_path}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    CACHE_DIR.mkdir(exist_ok=True)
    font_path = find_cjk_font()

    session = requests.Session()

    # Discover which URL pattern works
    print(f"[*] Probing URL pattern with chapter {CHAPTER_IDS[0]}…")
    url_pattern = probe_url_pattern(session, CHAPTER_IDS[0])

    total   = len(CHAPTER_IDS)
    chapters = []  # list of (title, body, chapter_id)
    errors   = []

    print(f"\n[*] Fetching {total} chapters (cache dir: {CACHE_DIR}/)…\n")
    for i, cid in enumerate(CHAPTER_IDS, 1):
        try:
            title, body = load_or_fetch(session, url_pattern, cid)
            chapters.append((title, body, cid))
            cached = "cache" if (CACHE_DIR / f"{cid}.txt").exists() else "fetched"
            print(f"  [{i:3}/{total}] {cid}  {title[:40]}  ({cached})")
        except Exception as e:
            print(f"  [{i:3}/{total}] {cid}  ERROR: {e}")
            errors.append((cid, str(e)))

    if not chapters:
        print("\n[!] No chapters fetched. Exiting.")
        sys.exit(1)

    print(f"\n[*] Fetched {len(chapters)}/{total} chapters"
          + (f"  ({len(errors)} errors)" if errors else ""))

    if errors:
        print("[!] Failed chapters:")
        for cid, err in errors:
            print(f"    {cid}: {err}")

    print(f"\n[*] Building PDF…")
    build_pdf(chapters, font_path, OUTPUT_PDF)
    size_mb = os.path.getsize(OUTPUT_PDF) / 1_048_576
    print(f"[✓] Done!  {OUTPUT_PDF}  ({size_mb:.1f} MB, {len(chapters)} chapters)")


if __name__ == "__main__":
    main()
