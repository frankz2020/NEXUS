from __future__ import annotations

import base64
import io
import json
import logging
from functools import lru_cache

import requests
from PIL import Image

from ..core import config
from ..utils import openrouter_client

logger = logging.getLogger("cover_image_filter")

UI_KEYWORDS = {
    "download",
    "close",
    "open",
    "preview",
    "share",
    "png",
    "jpg",
    "jpeg",
    "pdf",
}

BRANDING_TEXT_KEYWORDS = {
    "enterprise",
    "daily",
    "news",
    "times",
    "journal",
    "tribune",
    "herald",
    "gazette",
    "chronicle",
    "observer",
    "edition",
}

BRANDING_URL_HINTS = (
    "tncms/custom/image",
    "townnews.com/content/tncms/custom/image",
    "/content/tncms/custom/image/",
    "/custom/image/",
    "custom/image",
)


def _image_bytes_to_data_uri(image_bytes: bytes, content_type: str) -> str:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{content_type};base64,{b64}"


def _prepare_image_bytes(image_bytes: bytes) -> tuple[bytes, str] | None:
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img = img.convert("RGB")
            max_side = 1400
            if max(img.size) > max_side:
                img.thumbnail((max_side, max_side))

            out = io.BytesIO()
            img.save(out, format="JPEG", quality=85, optimize=True)
            return out.getvalue(), "image/jpeg"
    except Exception as exc:
        logger.warning("[IMAGE_FILTER] Failed to prepare image bytes: %s", exc)
        return None


def _extract_json_object(raw_text: str) -> dict | None:
    if not raw_text:
        return None

    raw_text = raw_text.strip()
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        pass

    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    try:
        return json.loads(raw_text[start:end + 1])
    except json.JSONDecodeError:
        return None


def _normalize_keywords(value) -> list[str]:
    if not isinstance(value, list):
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        word = item.strip().lower()
        if not word or word not in UI_KEYWORDS or word in seen:
            continue
        seen.add(word)
        normalized.append(word)
    return normalized


def _default_metrics() -> dict:
    return {
        "visible_text": "",
        "ocr_char_count": 0,
        "line_count": 0,
        "paragraph_count": 0,
        "text_coverage_ratio": 0.0,
        "ui_keywords": [],
    }


def _normalize_visible_text(value: str) -> str:
    return " ".join((value or "").lower().replace("\n", " ").split())


def _tokenize_visible_text(value: str) -> list[str]:
    text = _normalize_visible_text(value)
    cleaned = []
    current = []
    for ch in text:
        if ch.isalpha():
            current.append(ch)
        elif current:
            cleaned.append("".join(current))
            current = []
    if current:
        cleaned.append("".join(current))
    return cleaned


def _looks_like_branding_text_only(metrics: dict) -> bool:
    tokens = _tokenize_visible_text(metrics["visible_text"])
    if not tokens:
        return False

    meaningful_tokens = [tok for tok in tokens if len(tok) >= 3]
    if not meaningful_tokens:
        return False

    branding_token_count = sum(1 for tok in meaningful_tokens if tok in BRANDING_TEXT_KEYWORDS)
    return branding_token_count >= max(1, len(meaningful_tokens) - 1)


def _looks_like_branding_image(image_url: str, metrics: dict) -> bool:
    low_text = metrics["ocr_char_count"] <= 90
    few_lines = metrics["line_count"] <= 4
    no_paragraphs = metrics["paragraph_count"] <= 1
    low_coverage = metrics["text_coverage_ratio"] <= 0.18
    visible_text = _normalize_visible_text(metrics["visible_text"])
    has_branding_word = any(word in visible_text for word in BRANDING_TEXT_KEYWORDS)
    branding_text_only = _looks_like_branding_text_only(metrics)
    url_hint = any(hint in (image_url or "").lower() for hint in BRANDING_URL_HINTS)
    return low_text and few_lines and no_paragraphs and low_coverage and (
        branding_text_only or has_branding_word or url_hint
    )


@lru_cache(maxsize=256)
def inspect_image_url(image_url: str) -> dict:
    metrics = _default_metrics()
    if not image_url or not config.OPENROUTER_API_KEY:
        return metrics

    try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) Chrome/124 Safari/537.36"}
        response = requests.get(image_url, headers=headers, timeout=12)
        response.raise_for_status()
        prepared = _prepare_image_bytes(response.content)
        if not prepared:
            return metrics

        image_bytes, content_type = prepared
        prompt = """Extract OCR-style signals from this image and return strict JSON only.

Return exactly one JSON object with these keys:
- visible_text: string, first 300 visible characters only
- ocr_char_count: integer, estimated count of readable text characters excluding spaces
- line_count: integer, count of distinct readable text lines
- paragraph_count: integer, count of paragraph-like text blocks with 20+ characters
- text_coverage_ratio: number from 0 to 1 estimating how much of the image area is occupied by readable text
- ui_keywords: array containing only the exact visible UI/file words from this allowlist when present: ["Download", "Close", "Open", "Preview", "Share", "png", "jpg", "jpeg", "pdf"]

Do not classify the image type. Do not explain your reasoning. Return JSON only."""

        raw = openrouter_client.generate_content_from_messages(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": _image_bytes_to_data_uri(image_bytes, content_type)}},
                    ],
                }
            ],
            model=config.OPENROUTER_IMAGE_FILTER_MODEL,
            temperature=0,
        )
        parsed = _extract_json_object(raw or "")
        if not parsed:
            logger.warning("[IMAGE_FILTER] Failed to parse JSON for image %s", image_url)
            return metrics

        metrics["visible_text"] = str(parsed.get("visible_text", "")).strip()[:300]
        metrics["ocr_char_count"] = max(0, int(parsed.get("ocr_char_count", 0) or 0))
        metrics["line_count"] = max(0, int(parsed.get("line_count", 0) or 0))
        metrics["paragraph_count"] = max(0, int(parsed.get("paragraph_count", 0) or 0))
        try:
            coverage = float(parsed.get("text_coverage_ratio", 0) or 0)
        except (TypeError, ValueError):
            coverage = 0.0
        metrics["text_coverage_ratio"] = max(0.0, min(1.0, coverage))
        metrics["ui_keywords"] = _normalize_keywords(parsed.get("ui_keywords"))
        return metrics
    except Exception as exc:
        logger.warning("[IMAGE_FILTER] Inspection failed for %s: %s", image_url, exc)
        return metrics


def should_skip_image_url(image_url: str) -> tuple[bool, str, dict]:
    metrics = inspect_image_url(image_url)
    if _looks_like_branding_image(image_url, metrics):
        return True, "branding_image", metrics

    if metrics["ui_keywords"]:
        return True, "ui_keywords", metrics

    if (
        metrics["ocr_char_count"] >= config.IMAGE_FILTER_OCR_CHAR_THRESHOLD
        and metrics["line_count"] >= config.IMAGE_FILTER_LINE_THRESHOLD
    ):
        return True, "dense_text", metrics

    if (
        metrics["ocr_char_count"] >= config.IMAGE_FILTER_OCR_CHAR_THRESHOLD
        and metrics["paragraph_count"] >= config.IMAGE_FILTER_PARAGRAPH_THRESHOLD
    ):
        return True, "paragraph_text", metrics

    if (
        metrics["ocr_char_count"] >= config.IMAGE_FILTER_MIN_CHARS_FOR_COVERAGE
        and metrics["text_coverage_ratio"] >= config.IMAGE_FILTER_TEXT_COVERAGE_THRESHOLD
    ):
        return True, "text_coverage", metrics

    return False, "", metrics
