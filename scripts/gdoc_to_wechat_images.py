# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import os
import re
import sys
import json
import pickle
import requests
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin
from bs4 import BeautifulSoup

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request, AuthorizedSession

from news_bot.processing.image_generator import (
    generate_image_from_article,
    make_reference_image_from_reports,
)

SCHOOL_BRAND_MAP = {
    "NYU": ("#57068c", "New York University (NYU)"),
    "NEW YORK UNIVERSITY": ("#57068c", "New York University (NYU)"),
    "USC": ("#990000", "University of Southern California"),
    "UNIVERSITY OF SOUTHERN CALIFORNIA": ("#990000", "University of Southern California"),
    "EMORY": ("#222c66", "Emory University"),
    "UCD": ("#022851", "University of California, Davis"),
    "UC DAVIS": ("#022851", "University of California, Davis"),
    "UNIVERSITY OF CALIFORNIA, DAVIS": ("#022851", "University of California, Davis"),
    "UBC": ("#002145", "University of British Columbia"),
    "UNIVERSITY OF BRITISH COLUMBIA": ("#002145", "University of British Columbia"),
    "EDINBURGH": ("#041e42", "University of Edinburgh"),
    "UNIVERSITY OF EDINBURGH": ("#041e42", "University of Edinburgh"),
}
DEFAULT_BRAND = ("#57068c", "Default (NYU)")

def pick_brand_from_title(doc_title: str) -> Tuple[str, str]:
    if not doc_title:
        return DEFAULT_BRAND
    t = doc_title.strip().upper()
    for key, val in SCHOOL_BRAND_MAP.items():
        if key in t:
            return val
    return DEFAULT_BRAND

def folder_for_school(matched_school_name: str) -> str:
    n = (matched_school_name or "").upper()
    if "NEW YORK UNIVERSITY" in n:     return "NYU_Weekly"
    if "SOUTHERN CALIFORNIA" in n:     return "USC_Weekly"
    if "EMORY" in n:                   return "EMORY_Weekly"
    if "DAVIS" in n:                   return "UCD_Weekly"
    if "BRITISH COLUMBIA" in n:        return "UBC_Weekly"
    if "EDINBURGH" in n:               return "EDIN_Weekly"
    return "Generic_Weekly"

SCOPES = [
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

ROOT = Path(__file__).resolve().parents[1]
CRED_FILE = (ROOT / "credentials.json").as_posix()
TOKEN_FILE = (ROOT / "token.pickle").as_posix()

def _get_creds() -> Credentials:
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "rb") as token:
                creds = pickle.load(token)
        except (pickle.UnpicklingError, UnicodeDecodeError, EOFError):
            try:
                creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            except (json.JSONDecodeError, UnicodeDecodeError):
                creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None

        if not creds:
            flow = InstalledAppFlow.from_client_secrets_file(CRED_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "wb") as token:
            pickle.dump(creds, token)

    return creds

def _extract_doc_id(arg: str) -> str:
    m = re.search(r"/document/d/([a-zA-Z0-9_-]+)", arg)
    return m.group(1) if m else arg.strip()

def fetch_doc(doc_id_or_url: str) -> Dict:
    doc_id = _extract_doc_id(doc_id_or_url)
    creds = _get_creds()
    session = AuthorizedSession(creds)

    fields = "body,inlineObjects,title"
    url = f"https://docs.googleapis.com/v1/documents/{doc_id}"
    resp = session.get(url, params={"fields": fields}, timeout=60)
    resp.raise_for_status()
    return resp.json()

def _get_text(paragraph: Dict) -> str:
    buf = []
    for e in paragraph.get("elements", []):
        tr = e.get("textRun")
        if tr and "content" in tr:
            buf.append(tr["content"])
    return "".join(buf).strip()

def _first_image_url(paragraph: Dict, inline_objects: Dict) -> str:
    for e in paragraph.get("elements", []):
        if "inlineObjectElement" in e:
            obj_id = e["inlineObjectElement"].get("inlineObjectId")
            if not obj_id:
                continue
            obj = inline_objects.get(obj_id, {})
            pic = obj.get("inlineObjectProperties", {}).get("embeddedObject", {})
            if "imageProperties" in pic:
                src = pic.get("imageProperties", {}).get("contentUri")
                if src:
                    return src
    return ""

def _all_links(paragraph: Dict) -> List[str]:
    out = []
    for e in paragraph.get("elements", []):
        tr = e.get("textRun")
        if not tr:
            continue
        link = tr.get("textStyle", {}).get("link")
        if link and link.get("url"):
            u = (link["url"] or "").strip()
            if u:
                out.append(u)
    return out

def _clean_paragraph_text(s: str) -> str:
    return s.replace("\r", "").strip()

def fetch_cover_from_source(page_url: str, timeout: int = 12) -> str:
    if not page_url:
        return ""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) Chrome/124 Safari/537.36"}
        r = requests.get(page_url, headers=headers, timeout=timeout)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        def _abs(u: str) -> str:
            return urljoin(page_url, (u or "").strip())

        for sel, attr in [
            ('meta[property="og:image"]', "content"),
            ('meta[name="og:image"]', "content"),
            ('meta[property="twitter:image"]', "content"),
            ('meta[name="twitter:image"]', "content"),
            ('meta[itemprop="image"]', "content"),
            ('link[rel="image_src"]', "href"),
        ]:
            tag = soup.select_one(sel)
            if tag and tag.get(attr):
                url = _abs(tag.get(attr))
                if _looks_like_image_url(url):
                    return url

        for img in soup.find_all("img"):
            src = _abs(img.get("src") or "")
            if not _looks_like_image_url(src):
                continue
            w = _to_int(img.get("width"))
            h = _to_int(img.get("height"))
            if (w and w < 240) or (h and h < 160):
                continue
            return src
    except Exception as e:
        print(f"Warning: Failed to fetch cover from {page_url}: {e}")
    return ""

def _looks_like_image_url(url: str) -> bool:
    if not url:
        return False
    bad = (".svg", ".gif", "data:image/svg", "sprite", "logo", "icon")
    u = url.lower()
    return not any(b in u for b in bad)

def _to_int(x) -> Optional[int]:
    try:
        return int(str(x).strip())
    except (ValueError, AttributeError):
        return None

def _looks_like_source_line(s: str) -> bool:
    low = s.lower().strip()
    if low.startswith("来源") or low.startswith("source"):
        return True
    if low.startswith("来源 (source)"):
        return True
    return False

def parse_news_from_doc(doc: Dict, extract_images: bool = True) -> List[Dict]:
    content = doc.get("body", {}).get("content", [])
    inline_objects = doc.get("inlineObjects", {}) or {}

    items: List[Dict] = []
    cur: Optional[Dict] = None

    for blk in content:
        p = blk.get("paragraph")
        if not p:
            continue

        style = p.get("paragraphStyle", {}).get("namedStyleType", "")
        if style == "HEADING_1":
            title = _get_text(p).strip()
            if title:
                if cur and (cur.get("title") and cur.get("content", "").strip()):
                    items.append(cur)
                cur = {
                    "title": title,
                    "content": "",
                    "source_url": "",
                    "source_urls": [],
                    "cover_image": "",
                }
            continue

        if cur is None:
            continue

        if extract_images and not cur.get("cover_image"):
            img = _first_image_url(p, inline_objects)
            if img:
                cur["cover_image"] = img

        links = _all_links(p)
        if links:
            for u in links:
                if u not in cur["source_urls"]:
                    cur["source_urls"].append(u)
            if not cur["source_url"]:
                cur["source_url"] = cur["source_urls"][0]

        txt = _clean_paragraph_text(_get_text(p))
        if txt:
            if _looks_like_source_line(txt):
                continue
            if cur["content"]:
                cur["content"] += "\n\n" + txt
            else:
                cur["content"] = txt

    if cur and (cur.get("title") and cur.get("content", "").strip()):
        items.append(cur)

    for it in items:
        it["content"] = it["content"].strip()
    return items

def _slug(s: str) -> str:
    s = re.sub(r"[^\w\-]+", "_", s.strip())
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "Doc"

def _infer_school_dir(title: str) -> str:
    m = re.search(r"[A-Za-z]{2,}", title or "")
    base = m.group(0).upper() if m else "SCHOOL"
    return f"{base}_Weekly"

def render_to_images(
    items: List[Dict],
    *,
    doc_title: str,
    out_dir: str,
    page_width: int,
    device_scale: int,
    brand_color: Optional[str],
    title_size: float,
    body_size: float,
    top_n: int,
    skip_image_fetch: bool = False,
    school_name: str = "",
) -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from news_bot.processing.image_generator import BrowserContext, _render_html

    out_root = Path(out_dir)
    school_dir = folder_for_school(school_name) if school_name else _infer_school_dir(doc_title)

    if out_root.name == school_dir:
        school_out = out_root
    else:
        school_out = out_root / school_dir

    school_out.mkdir(parents=True, exist_ok=True)

    upper_name = (school_name or "").upper()
    is_ucd = ("DAVIS" in upper_name) or ("UC DAVIS" in upper_name) or ("UCD" in upper_name)

    def fetch_cover_for_item(idx_item):
        idx, it = idx_item
        cover_image = it.get("cover_image") or ""
        if not cover_image and not skip_image_fetch:
            src_url = (it.get("source_url") or "").strip()
            if src_url:
                print(f"  [{idx}] Fetching cover from: {src_url[:50]}...")
                cover_image = fetch_cover_from_source(src_url)
        return idx, cover_image

    cover_images = {}
    items_needing_fetch = [(i, it) for i, it in enumerate(items, 1)
                           if not it.get("cover_image") and not skip_image_fetch]

    if items_needing_fetch:
        print(f"  [*] Fetching {len(items_needing_fetch)} cover images in parallel...")
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(fetch_cover_for_item, item): item for item in items_needing_fetch}
            for future in as_completed(futures):
                try:
                    idx, cover = future.result()
                    if cover:
                        cover_images[idx] = cover
                except Exception as e:
                    print(f"  [!] Error fetching cover: {e}")

    print(f"  [*] Rendering {len(items)} images with shared browser...")

    with BrowserContext() as browser_ctx:
        for idx, it in enumerate(items, 1):
            left_bar_color = None
            if is_ucd:
                left_bar_color = "#022851" if (idx % 2 == 1) else "#FFBF00"

            title = it.get("title", "").strip()
            content = it.get("content", "").strip()
            cover_image = it.get("cover_image") or cover_images.get(idx, "")

            out_png = school_out / f"{idx:02d}_{_slug(title)[:40]}.png"

            html = _render_html(
                title=title,
                content=content,
                credits="",
                cover_image=cover_image,
                cover_caption="",
                page_width=page_width,
                min_height=2200,
                title_size=title_size,
                body_size=body_size,
                marker_label="",
                brand_color=brand_color or "#57068c",
                left_bar_color=left_bar_color,
            )

            browser_ctx.render(html, out_png, page_width, device_scale)
            print(f"  [✓] {idx}/{len(items)}: {title[:30]}...")

    if top_n and top_n > 0:
        flat_urls: List[str] = []
        seen = set()
        for it in items:
            multi = it.get("source_urls") or []
            if multi:
                for u in multi:
                    u = (u or "").strip()
                    if u and u not in seen:
                        seen.add(u)
                        flat_urls.append(u)
            else:
                u = (it.get("source_url") or "").strip()
                if u and u not in seen:
                    seen.add(u)
                    flat_urls.append(u)

        if flat_urls:
            import tempfile
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tf:
                json.dump([{"source_url": u} for u in flat_urls], tf, ensure_ascii=False, indent=2)
                tmp_path = tf.name

            try:
                make_reference_image_from_reports(
                    sorted_json_path=tmp_path,
                    output_dir=str(school_out),
                    filename="00_资料来源.png",
                    top_n=min(top_n, len(flat_urls)),
                    page_width=page_width,
                    device_scale=device_scale,
                    brand_color=brand_color or "#57068c",
                )
            finally:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Render a single weekly Google Doc into WeChat-style images."
    )
    p.add_argument("--doc", required=True, help="Google Doc URL or docId")
    p.add_argument("--out", default="wechat_images")
    p.add_argument("--page-width", type=int, default=540)
    p.add_argument("--device-scale", type=int, default=4)
    p.add_argument("--title-size", type=float, default=22.5)
    p.add_argument("--body-size", type=float, default=22.5)
    p.add_argument("--brand-color", default="")
    p.add_argument("--top-n", type=int, default=10, help="How many source links to show on reference page")
    p.add_argument("--no-images", action="store_true", help="Skip fetching/using cover images")
    return p

def main():
    args = build_argparser().parse_args()
    doc = fetch_doc(args.doc)
    doc_title = (doc.get("title") or "").strip()

    auto_color, school_name = pick_brand_from_title(doc_title)
    brand_color = args.brand_color.strip() or auto_color

    print(f"Doc title: {doc_title}")
    print(f"识别学校：{school_name}  brand_color={brand_color}")

    items = parse_news_from_doc(doc, extract_images=not args.no_images)
    if not items:
        print("No items parsed; nothing to render.")
        return

    render_to_images(
        items,
        doc_title=doc_title,
        out_dir=args.out,
        page_width=args.page_width,
        device_scale=args.device_scale,
        brand_color=brand_color,
        title_size=args.title_size,
        body_size=args.body_size,
        top_n=args.top_n,
        skip_image_fetch=args.no_images,
        school_name=school_name,
    )
    print("Done.")

if __name__ == "__main__":
    main()
