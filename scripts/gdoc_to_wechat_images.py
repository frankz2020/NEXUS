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

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build  # type: ignore

# 使用你的渲染器
from news_bot.processing.image_generator import (
    generate_image_from_article,
    make_reference_image_from_reports,
)

# =================== 学校 → 品牌色 & 文件夹名 ===================
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

SCOPES = ["https://www.googleapis.com/auth/documents.readonly",
          "https://www.googleapis.com/auth/drive.readonly"]

ROOT = Path(__file__).resolve().parents[1]
CRED_FILE = (ROOT / "credentials.json").as_posix()
TOKEN_FILE = (ROOT / "token.pickle").as_posix()


# -------------------------
# Google Docs helpers
# -------------------------
def _build_docs_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            # Try to load as pickle first (compatible with other scripts)
            with open(TOKEN_FILE, "rb") as token:
                creds = pickle.load(token)
        except (pickle.UnpicklingError, UnicodeDecodeError, EOFError):
            # If pickle fails, try JSON format
            try:
                creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            except (json.JSONDecodeError, UnicodeDecodeError):
                # If both fail, token file is corrupted, will re-authenticate
                creds = None
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                # Refresh failed, need to re-authenticate
                creds = None
        
        if not creds:
            flow = InstalledAppFlow.from_client_secrets_file(CRED_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        
        # Save as pickle (compatible with other scripts)
        with open(TOKEN_FILE, "wb") as token:
            pickle.dump(creds, token)
    
    return build("docs", "v1", credentials=creds, cache_discovery=False)


def _extract_doc_id(arg: str) -> str:
    m = re.search(r"/document/d/([a-zA-Z0-9_-]+)", arg)
    return m.group(1) if m else arg.strip()


def fetch_doc(doc_id_or_url: str) -> Dict:
    doc_id = _extract_doc_id(doc_id_or_url)
    svc = _build_docs_service()
    fields = "body,inlineObjects,title"
    return svc.documents().get(documentId=doc_id, fields=fields).execute()  # type: ignore


# -------------------------
# Parsing helpers
# -------------------------
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


_URL_RE = re.compile(r"https?://[^\s\)\]\}，。；、]+", re.IGNORECASE)


def _urls_from_text(s: str) -> List[str]:
    if not s:
        return []
    return [m.group(0) for m in _URL_RE.finditer(s)]


def fetch_cover_from_source(page_url: str, timeout: int = 12) -> str:
    """
    从来源页面抓取封面图片（og:image, twitter:image 等）
    """
    if not page_url:
        return ""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) Chrome/124 Safari/537.36"}
        r = requests.get(page_url, headers=headers, timeout=timeout)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        def _abs(u: str) -> str:
            return urljoin(page_url, (u or "").strip())

        # 优先尝试 og:image, twitter:image 等 meta 标签
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

        # 如果没有找到 meta 标签，尝试找页面中的大图
        for img in soup.find_all("img"):
            src = _abs(img.get("src") or "")
            if not _looks_like_image_url(src):
                continue
            w = _to_int(img.get("width"))
            h = _to_int(img.get("height"))
            # 只选择足够大的图片（避免 logo 等小图）
            if (w and w < 240) or (h and h < 160):
                continue
            return src
    except Exception as e:
        print(f"Warning: Failed to fetch cover from {page_url}: {e}")
    return ""


def _looks_like_image_url(url: str) -> bool:
    """判断 URL 是否看起来像图片"""
    if not url:
        return False
    bad = (".svg", ".gif", "data:image/svg", "sprite", "logo", "icon")
    u = url.lower()
    return not any(b in u for b in bad)


def _to_int(x) -> Optional[int]:
    """安全转换为整数"""
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
    
    title_raw = doc.get("title", "Untitled")
    print(f"[DEBUG] Parsing doc: {title_raw}")

    items: List[Dict] = []
    cur: Optional[Dict] = None

    for i, blk in enumerate(content):
        p = blk.get("paragraph")
        if not p:
            continue

        style = p.get("paragraphStyle", {}).get("namedStyleType", "")
        text_content = _get_text(p).strip()
        
        # Debug heading detection
        if style == "HEADING_1":
            print(f"[DEBUG] Found HEADING_1 at index {i}: {text_content[:30]}...")
            
            title = text_content
            if title:
                if cur and (cur.get("title") and cur.get("content", "").strip()):
                    print(f"[DEBUG] Finishing article: {cur['title'][:20]}... (len={len(cur['content'])})")
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
            # print(f"[DEBUG] Skipping content before first heading at index {i}: {text_content[:20]}...")
            continue


        if extract_images and not cur.get("cover_image"):
            img = _first_image_url(p, inline_objects)
            if img:
                cur["cover_image"] = img

        links = _all_links(p)
        # Also capture plain-text URLs (not hyperlink-formatted)
        links += _urls_from_text(text_content)
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


# -------------------------
# Rendering
# -------------------------
def _slug(s: str) -> str:
    s = re.sub(r"[^\w\-]+", "_", s.strip())
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "Doc"


def _infer_school_dir(title: str) -> str:
    # 你项目里通常是 NYU_Weekly / UCD_Weekly 等
    # 这里做个稳妥兜底：取标题首个英数词 + "_Weekly"
    m = re.search(r"[A-Za-z]{2,}", title or "")
    base = m.group(0).upper() if m else "SCHOOL"
    return f"{base}_Weekly"


def _normalize_content(content: str, min_chars_per_paragraph: int = 50) -> str:
    """
    标准化内容格式，确保分段正确。
    规则：每50字以上 + 句末标点自动分段
    
    句末标点包括：。？！…
    标点后可跟引号：」』""'）】》
    注意：允许句号和引号之间有空白，防止因空格导致引号被甩到下一段
    """
    # 移除所有换行，合并为单行，但保留空格以便后续正则能匹配到间隔
    text = content.replace('\r', '').replace('\n', '')
    # 将多个空格合并为一个，避免过长空白
    text = re.sub(r'\s+', ' ', text)
    
    # 句末标点 + (可选空白) + (可选的后引号/括号)
    # 匹配模式：
    # 1. 句末标点：。？！…
    # 2. 可选空白：\s*
    # 3. 后引号/括号：」』"'）】》 + 中文双引号 U+201C/U+201D
    # 使用 Unicode 转义确保中文双引号被正确匹配
    sentence_end_pattern = r'(?:[。？！]|\.{3}|…{1,2})\s*[」』\u201c\u201d"\'）】》]*'
    
    result = []
    current_paragraph = ""
    last_end = 0
    
    # 找到所有句子结束位置
    for match in re.finditer(sentence_end_pattern, text):
        # 从上次结束位置到这次匹配结束（包含标点和引号）
        segment = text[last_end:match.end()]
        current_paragraph += segment
        last_end = match.end()
        
        # 检查是否需要分段：当前段落超过 min_chars_per_paragraph
        if len(current_paragraph) >= min_chars_per_paragraph:
            # strip() 会去掉段落首尾空格，也能去掉句末引号后的空格
            result.append(current_paragraph.strip())
            current_paragraph = ""
    
    # 处理剩余内容
    if last_end < len(text):
        current_paragraph += text[last_end:]
    
    if current_paragraph.strip():
        result.append(current_paragraph.strip())
    
    # 用双换行连接段落
    return '\n\n'.join(result)


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
) -> List[str]:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from news_bot.processing.image_generator import BrowserContext, _render_html

    out_root = Path(out_dir)
    
    # 逻辑修正：如果 out_dir 的末尾已经是 school_dir 了，就不再嵌套
    school_dir = folder_for_school(school_name) if school_name else _infer_school_dir(doc_title)
    
    if out_root.name == school_dir:
        school_out = out_root
    else:
        school_out = out_root / school_dir
        
    school_out.mkdir(parents=True, exist_ok=True)
    generated_paths: List[str] = []

    upper_name = (school_name or "").upper()
    # 命中任意一个都算 UCD（用于交替色）
    is_ucd = ("DAVIS" in upper_name) or ("UC DAVIS" in upper_name) or ("UCD" in upper_name)

    # ========== OPTIMIZATION 1: Parallel cover image fetching ==========
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
    
    # ========== OPTIMIZATION 2: Batch rendering with browser reuse ==========
    print(f"  [*] Rendering {len(items)} images with shared browser...")
    
    with BrowserContext() as browser_ctx:
        for idx, it in enumerate(items, 1):
            left_bar_color = None
            if is_ucd:
                left_bar_color = "#022851" if (idx % 2 == 1) else "#FFBF00"

            title = it.get("title", "").strip()
            content = _normalize_content(it.get("content", "").strip())
            
            # Use pre-fetched cover or existing one
            cover_image = it.get("cover_image") or cover_images.get(idx, "")

            out_png = school_out / f"{idx:02d}_{_slug(title)[:40]}.png"

            # Render HTML
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
            
            # Render with shared browser
            browser_ctx.render(html, out_png, page_width, device_scale)
            print(f"  [✓] {idx}/{len(items)}: {title[:30]}...")
            generated_paths.append(str(out_png))

    # 生成"资料来源"汇总页（基于所有文章的全部链接扁平化）
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
            # 使用系统临时目录，确保不会在输出文件夹留下痕迹
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tf:
                json.dump([{"source_url": u} for u in flat_urls], tf, ensure_ascii=False, indent=2)
                tmp_path = tf.name

            try:
                ref_path = make_reference_image_from_reports(
                    sorted_json_path=tmp_path,
                    output_dir=str(school_out),
                    filename="00_资料来源.png",
                    top_n=min(top_n, len(flat_urls)),
                    page_width=page_width,
                    device_scale=device_scale,
                    brand_color=brand_color or "#57068c",
                )
                generated_paths.append(ref_path)
            finally:
                # 显式删除临时文件
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
    
    return generated_paths


# -------------------------
# CLI
# -------------------------
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
