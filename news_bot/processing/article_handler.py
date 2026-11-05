# news_bot/processing/article_handler.py

import requests
import os
import logging
import time
from bs4 import BeautifulSoup
from datetime import datetime, date, timedelta
import json
import re # For URL date parsing
from ..discovery.date_extractor import extract_date_from_url
from ..utils import prompt_logger, openrouter_client
from ..utils import http_fetcher

from ..core import config

# Setup logging
logger = logging.getLogger('article_handler')

MIN_ARTICLE_WORDS = 80

# Matches whole class/id tokens (or dash/underscore-delimited parts of them) that mark
# non-article chrome. Substring matching is not safe here: '[class*="ad"]' also hits
# utility classes like 'leading-7', 'heading', 'shadow' and deleted real article bodies.
_NOISE_TOKEN_RE = re.compile(
    r'(?:^|[-_])(?:ad|ads|advert|adverts|advertisement|advertising|adsbygoogle|dfp|'
    r'sponsor|sponsored|popup|pop-up|modal|overlay|banner|cookie|cookies|consent|'
    r'share|sharing|sharedaddy|social-share|mask|paywall|newsletter-signup)(?:[-_]|$)',
    re.IGNORECASE,
)


def _is_noise_element(element) -> bool:
    tokens = list(element.get('class') or [])
    element_id = element.get('id')
    if element_id:
        tokens.append(element_id)
    return any(_NOISE_TOKEN_RE.search(token) for token in tokens)


def _pruned_copy(container) -> BeautifulSoup:
    """Return a detached copy of the container with non-article chrome removed."""
    container_soup = BeautifulSoup(str(container), 'html.parser')

    for unwanted_tag in container_soup(['script', 'style', 'nav', 'footer', 'aside', 'header', 'form', 'button', 'input', 'textarea', 'select', 'option']):
        unwanted_tag.decompose()

    for unwanted_element in container_soup.find_all(_is_noise_element):
        if not unwanted_element.decomposed:
            unwanted_element.decompose()
    return container_soup


def _paragraph_word_count(container) -> int:
    """Words in substantial <p> blocks: favours real article bodies over tag lists and comment rules."""
    container_soup = _pruned_copy(container)
    return sum(
        len(words)
        for words in (p.get_text(" ", strip=True).split() for p in container_soup.find_all('p'))
        if len(words) >= 8
    )


def _extract_text_from_container(container) -> str:
    """Extract the cleanest text we can from a candidate content container."""
    if not container:
        return ""

    container_soup = _pruned_copy(container)
    paragraphs = container_soup.find_all('p')
    paragraph_text = "\n".join(
        p.get_text(" ", strip=True)
        for p in paragraphs
        if p.get_text(" ", strip=True)
    )
    alternative_text = container_soup.get_text(separator='\n', strip=True)

    if len(alternative_text.split()) > len(paragraph_text.split()):
        return alternative_text
    return paragraph_text


def _iter_json_nodes(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_json_nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_json_nodes(item)


def _extract_text_from_jsonld(soup: BeautifulSoup) -> str | None:
    """Return article text from JSON-LD when available."""
    best_text = ""
    article_types = {"Article", "NewsArticle", "ReportageNewsArticle"}

    for script in soup.find_all('script', type='application/ld+json'):
        raw_json = script.string or script.get_text()
        if not raw_json or not raw_json.strip():
            continue
        try:
            data = json.loads(raw_json)
        except Exception:
            continue

        for item in _iter_json_nodes(data):
            item_type = item.get('@type') or item.get('type')
            if isinstance(item_type, str):
                item_types = [item_type]
            elif isinstance(item_type, list):
                item_types = [t for t in item_type if isinstance(t, str)]
            else:
                item_types = []

            if not any(t in article_types for t in item_types):
                continue

            candidate_text = item.get('articleBody') or item.get('text') or ""
            cleaned_text = "\n".join(
                line.strip() for line in str(candidate_text).splitlines() if line.strip()
            )
            if len(cleaned_text.split()) > len(best_text.split()):
                best_text = cleaned_text

    if len(best_text.split()) >= MIN_ARTICLE_WORDS:
        return best_text
    return None


def _extract_ubyssey_live_updates(soup: BeautifulSoup) -> str | None:
    """
    The Ubyssey renders live-blog articles client-side from embedded JSON
    (#page-info-at-load for title/lede, #updates-at-load for the posts, newest first).
    """
    updates_script = soup.find('script', id='updates-at-load')
    if not updates_script:
        return None
    try:
        updates = json.loads(updates_script.string or updates_script.get_text())
    except Exception:
        return None
    if not isinstance(updates, list):
        return None

    parts = []
    page_info_script = soup.find('script', id='page-info-at-load')
    if page_info_script:
        try:
            meta = json.loads(page_info_script.string or page_info_script.get_text()).get('meta') or {}
            parts.extend(str(meta[key]).strip() for key in ('title', 'lede') if meta.get(key))
        except Exception:
            pass

    for update in sorted(
        (u for u in updates if isinstance(u, dict)),
        key=lambda u: str(u.get('publish_date') or ''),
    ):
        update_text = _extract_text_from_container(BeautifulSoup(update.get('html') or '', 'html.parser'))
        if update_text.strip():
            parts.append(update_text.strip())

    text = "\n".join(parts)
    if len(text.split()) >= MIN_ARTICLE_WORDS:
        return text
    return None


def fetch_and_extract_text(url: str, _browser_retry: bool = False) -> str | None:
    """
    Fetches content from a URL and extracts clean textual content.

    If the plain HTTP fetch yields a thin page (fewer than MIN_ARTICLE_WORDS words),
    the page is fetched once more through the browser fallback in case the body is
    rendered client-side.
    """
    requested_url = url
    logger.info(f"[FETCH] Starting fetch for URL: {url}")
    print(f"Fetching and extracting text from: {url}")
    fetch_start = time.time()
    try:
        logger.debug(f"[FETCH] Sending GET request with timeout={config.URL_FETCH_TIMEOUT}s")
        fetched_page = http_fetcher.fetch_page(
            url,
            timeout=config.URL_FETCH_TIMEOUT,
            force_browser=_browser_retry,
        )
        logger.debug(
            f"[FETCH] Response status: {fetched_page.status_code}, "
            f"content-length: {len(fetched_page.content)}, source={fetched_page.source}"
        )
        url = fetched_page.url
        soup = BeautifulSoup(fetched_page.content, 'html.parser')
        logger.debug(f"[FETCH] HTML parsed successfully")
        # if read full button found, extract the text from button's url (apply to UBC)
        try:
            read_full_button = soup.find('a', string='Read the full message')
            if read_full_button:
                from urllib.parse import urljoin

                read_full_url = urljoin(url, read_full_button['href'])
                print(f"DEBUG: read full button found, url: {read_full_url}\n")
                fetched_page = http_fetcher.fetch_page(
                    read_full_url,
                    timeout=config.URL_FETCH_TIMEOUT,
                    referer=url,
                )
                url = fetched_page.url
                soup = BeautifulSoup(fetched_page.content, 'html.parser')
        except Exception as e:
            print(f"not read full button found")
            pass
        
        # --- Domain-specific extraction: The Student News (avoid hidden trending preview article) ---
        article_body = None
        try:
            from urllib.parse import urlparse
            domain = urlparse(url).netloc
        except Exception:
            domain = ''

        if 'thestudentnews.co.uk' in domain:
            # Prefer the real article content container inside the primary content area
            tsn_selectors = [
                '#content #primary article .entry-content',
                '#primary .entry-content',
                'main.site-main article .entry-content',
                '.single-post .entry-content',
                '.entry-content'
            ]
            for css in tsn_selectors:
                el = soup.select_one(css)
                if el:
                    article_body = el
                    print(f"DEBUG: thestudentnews extractor using selector: '{css}'")
                    break
        
        # --- Domain-specific extraction: LA Times (robust against paywall/teasers) ---
        if not article_body and 'latimes.com' in domain:
            # 1) Try JSON-LD 'articleBody' which LA Times provides with full text
            try:
                import json as _json
                article_text_from_jsonld = None
                for script in soup.find_all('script', type='application/ld+json'):
                    try:
                        data = _json.loads(script.string or "")
                    except Exception:
                        continue
                    # JSON-LD may be a dict or a list of dicts
                    candidates = data if isinstance(data, list) else [data]
                    for item in candidates:
                        if not isinstance(item, dict):
                            continue
                        item_type = item.get('@type') or item.get('type')
                        if isinstance(item_type, list):
                            item_type = next((t for t in item_type if isinstance(t, str)), None)
                        if item_type in {'NewsArticle', 'Article'}:
                            body = item.get('articleBody') or item.get('text')
                            if body and len(body.split()) > 100:
                                article_text_from_jsonld = body
                                break
                    if article_text_from_jsonld:
                        break
                if article_text_from_jsonld:
                    cleaned = "\n".join([ln.strip() for ln in str(article_text_from_jsonld).splitlines() if ln.strip()])
                    print("DEBUG: LA Times extractor used JSON-LD articleBody")
                    return cleaned
            except Exception as _e_lat_json:
                print(f"DEBUG: LA Times JSON-LD parse failed: {_e_lat_json}")
                pass
            
            # 2) Fallback to AMP version (?outputType=amp) which is static HTML
            try:
                from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
                parts = urlsplit(url)
                query_pairs = dict(parse_qsl(parts.query, keep_blank_values=True))
                if 'outputType' not in query_pairs:
                    query_pairs['outputType'] = 'amp'
                amp_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_pairs), parts.fragment))
                if amp_url != url:
                    print(f"DEBUG: Fetching LA Times AMP page: {amp_url}")
                    amp_page = http_fetcher.fetch_page(
                        amp_url,
                        timeout=config.URL_FETCH_TIMEOUT,
                        referer=url,
                    )
                    amp_soup = BeautifulSoup(amp_page.content, 'html.parser')
                    # Prefer focused article containers; otherwise read from <main> or <article>
                    amp_selectors = [
                        '[data-qa="article-body"]',
                        'div.article-body',
                        'article .article-body',
                        'article',
                        'main'
                    ]
                    for css in amp_selectors:
                        el = amp_soup.select_one(css)
                        if el:
                            article_body = el
                            print(f"DEBUG: LA Times AMP extractor using selector: '{css}'")
                            break
            except Exception as _e_lat_amp:
                print(f"DEBUG: LA Times AMP fetch failed: {_e_lat_amp}")
                pass

        if not article_body and 'ubyssey.ca' in domain:
            ubyssey_text = _extract_ubyssey_live_updates(soup)
            if ubyssey_text:
                print(f"DEBUG: extractor used Ubyssey live updates JSON, url: {url}")
                logger.info(f"[FETCH] ✅ Successfully extracted text from {url}: {len(ubyssey_text.split())} words (Ubyssey live updates)")
                return ubyssey_text

        # Structured article data is often cleaner than the rendered layout.
        if not article_body:
            jsonld_text = _extract_text_from_jsonld(soup)
            if jsonld_text:
                print(f"DEBUG: extractor used JSON-LD articleBody/text, url: {url}")
                fetch_elapsed = time.time() - fetch_start
                logger.info(f"[FETCH] ✅ Successfully extracted text from {url}: {len(jsonld_text)} chars, {len(jsonld_text.split())} words (took {fetch_elapsed:.2f}s)")
                print(f"Successfully extracted text from {url} (approx. {len(jsonld_text.split())} words).")
                return jsonld_text

        # --- Generic extraction: score common content containers instead of taking the first hit ---
        if not article_body:
            main_content_tags = [
                '[itemprop="articleBody"]',
                '.entry-content',
                '.post-content',
                '.td-post-content',
                'article',
                'main',
            ]
            best_candidate = None
            best_selector = None
            best_word_count = 0
            best_score = (-1, -1)
            for selector in main_content_tags:
                for candidate in soup.select(selector):
                    candidate_word_count = len(_extract_text_from_container(candidate).split())
                    # Rank by words in real paragraphs first, total words as tie-breaker.
                    candidate_score = (_paragraph_word_count(candidate), candidate_word_count)
                    if candidate_score > best_score:
                        best_candidate = candidate
                        best_selector = selector
                        best_word_count = candidate_word_count
                        best_score = candidate_score
            if best_candidate and best_word_count >= MIN_ARTICLE_WORDS:
                article_body = best_candidate
                print(f"DEBUG: article_body found via generic selector '{best_selector}', url: {url}")
            elif best_candidate:
                print(f"DEBUG: best generic selector '{best_selector}' only had {best_word_count} words; using full <body>, url: {url}")
        
        if not article_body:
            article_body = soup.body

        text_content = _extract_text_from_container(article_body) if article_body else ""
        
        if not text_content.strip():
             print(f"Info: No significant text content from primary containers of {url}. Trying full soup minus known non-content tags.")
             for unwanted_tag in soup(['script', 'style', 'nav', 'footer', 'aside', 'header', 'form', 'button', 'input', 'textarea', 'select', 'option']):
                unwanted_tag.decompose()
             text_content = soup.get_text(separator='\n', strip=True)

        cleaned_text = "\n".join([line for line in text_content.splitlines() if line.strip()])

        # Runs before the empty-text bail-out so fully client-rendered pages (no text at all
        # in the raw HTML) also get the browser retry.
        if (
            len(cleaned_text.split()) < MIN_ARTICLE_WORDS
            and not _browser_retry
            and fetched_page.source != "playwright"
        ):
            logger.info(f"[FETCH] Thin content ({len(cleaned_text.split())} words) from {url}; retrying with browser rendering")
            print(f"Info: Only {len(cleaned_text.split())} words extracted from {url}; retrying with browser rendering.")
            browser_text = fetch_and_extract_text(requested_url, _browser_retry=True)
            if browser_text and len(browser_text.split()) > len(cleaned_text.split()):
                return browser_text

        if not cleaned_text.strip():
            logger.warning(f"[FETCH] No significant text content extracted from {url}")
            print(f"Warning: Still no significant text content extracted from {url}.")
            return None

        fetch_elapsed = time.time() - fetch_start
        logger.info(f"[FETCH] ✅ Successfully extracted text from {url}: {len(cleaned_text)} chars, {len(cleaned_text.split())} words (took {fetch_elapsed:.2f}s)")
        print(f"Successfully extracted text from {url} (approx. {len(cleaned_text.split())} words).")
        return cleaned_text

    except requests.exceptions.Timeout:
        fetch_elapsed = time.time() - fetch_start
        logger.error(f"[FETCH] Timeout after {fetch_elapsed:.2f}s for URL {url}")
        print(f"Error: Timeout while fetching URL {url}")
    except requests.exceptions.HTTPError as e:
        fetch_elapsed = time.time() - fetch_start
        logger.error(f"[FETCH] HTTP error {e.response.status_code} after {fetch_elapsed:.2f}s for URL {url}")
        print(f"Error: HTTP error {e.response.status_code} while fetching URL {url}")
    except requests.exceptions.RequestException as e:
        fetch_elapsed = time.time() - fetch_start
        logger.error(f"[FETCH] Request error after {fetch_elapsed:.2f}s for URL {url}: {e}")
        print(f"Error: Could not fetch URL {url}. Details: {str(e)}")
    except Exception as e:
        fetch_elapsed = time.time() - fetch_start
        logger.error(f"[FETCH] Unexpected error after {fetch_elapsed:.2f}s for URL {url}: {e}")
        import traceback
        logger.error(f"[FETCH] Traceback: {traceback.format_exc()}")
        print(f"Error: An unexpected error occurred while fetching/processing URL {url}: {str(e)}")
    return None


def verify_article_with_gemini(school: dict[str, str], article_text: str, article_url: str, publication_date: str) -> dict | None:
    """
    Verifies an article using Gemini for date extraction and article type assessment.
    Relevance is already filtered in Step 1, so this step focuses on date accuracy and article classification.
    Uses the configured date range from config.get_news_date_range().
    Supplements Gemini date extraction with URL parsing if Gemini fails.
    Uses Gemini 2.5 Pro for better accuracy.
    """
    logger.info(f"[VERIFY] Starting verification for: {article_url[:80]}...")
    logger.debug(f"[VERIFY] Article text length: {len(article_text) if article_text else 0}")
    logger.debug(f"[VERIFY] Publication date from URL: {publication_date}")
    
    if not config.OPENROUTER_API_KEY:
        logger.error("[VERIFY] OPENROUTER_API_KEY not configured")
        print("Error: OPENROUTER_API_KEY not configured for verification.")
        return None
    
    # Get the configured date range
    start_date, end_date = config.get_news_date_range()
    logger.debug(f"[VERIFY] Date range for filtering: {start_date} to {end_date}")
    
    if not article_text or not article_text.strip():
        logger.warning(f"[VERIFY] Empty article text for {article_url}")
        print(f"Info: Skipping Gemini verification for {article_url} due to empty article text.")
        publication_date_from_url = publication_date
        final_date_str = publication_date_from_url if publication_date_from_url else "Date not found"
        is_recent_status = "Date unclear (no text/URL date)"
        is_within_range = False
        
        if publication_date_from_url:
            try:
                pub_dt = datetime.strptime(publication_date_from_url, "%Y-%m-%d").date()
                if pub_dt >= start_date and pub_dt <= end_date:
                    is_recent_status = f"Within range (from URL, {start_date} to {end_date})"
                    is_within_range = True
                elif pub_dt > end_date:
                    is_recent_status = f"After range (from URL, after {end_date})"
                else:
                    is_recent_status = f"Before range (from URL, before {start_date})"
            except ValueError:
                is_recent_status = "Date unparsable (from URL)"
        
        return {
            "url": article_url,
            "publication_date_str": final_date_str,
            "is_recent": is_recent_status,
            "is_within_range": is_within_range,
            "is_relevant": "Relevant",  # Already filtered in Step 1
            "article_type_assessment": "Type unclear (no text)"
        }

    print(f"Verifying article with OpenRouter ({config.OPENROUTER_MODEL}): {article_url[:100]}...")
    print(f"Using date range: {start_date} to {end_date}")

    context_limit = getattr(config, 'OPENROUTER_MODEL_CONTEXT_LIMIT_CHARS', 2000000)
    article_text_limit = min(len(article_text), context_limit // 2)  # Use half for safety
    
    sensitive_topics_text = "; ".join(config.SENSITIVE_TOPICS) or "none"
    prompt = f"""You are a news article analyst. Your task is to extract key metadata from the article text.

## Requirements

### Accuracy & Factuality (Highest Priority)
- Extract dates ONLY from the article text or URL - do not infer or guess dates
- Base article type classification strictly on the content structure and purpose
- Do not add information not present in the article

### Task
Analyze the article and provide your analysis in EXACTLY four lines, each starting with the specified prefix:

1. Publication Date: [Review the article text AND the Article URL ({article_url}). Extract the most prominent date, ideally the publication date. Format YYYY-MM-DD or 'Date not found'. No other explanation.]
2. Article Type: [Is this primarily a news article reporting on events/facts, or an opinion/blog/event listing/announcement? Answer ONLY 'News article', 'Opinion/Blog', 'Event/Announcement', or 'Type unclear'. No other explanation.]
3. Sensitive Topic: [This digest is published for readers in mainland China. Answer 'Yes' if the article's main subject, or a prominent part of it (headline, opening, or a quoted focus), involves any of these topics: {sensitive_topics_text}. Otherwise answer 'No'. Answer ONLY 'Yes' or 'No'.]
4. Analysis Notes: [Brief internal notes if needed, or 'N/A'. This line is for your process.]

## Input Data

--- Article Text (first {article_text_limit} characters) ---
{article_text[:article_text_limit]}
--- End of Article Text ---

Your response (exactly 4 lines as specified above):
"""

    gemini_publication_date_str = "Date not found" # Default from OpenRouter
    sensitive_topic_answer = None  # stays None unless the model gives a Yes/No
    try:
        print(f"Sending verification request to OpenRouter API ({config.OPENROUTER_MODEL})...")
        
        # Log the prompt
        prompt_logger.log_prompt(
            "verify_article_with_gemini",
            prompt,
            context={
                "article_url": article_url,
                "publication_date": publication_date,
                "school": school.get('school_name', 'Unknown'),
                "article_text_length": len(article_text)
            }
        )
        
        raw_response_text = openrouter_client.generate_content(
            prompt=prompt,
            model=config.OPENROUTER_MODEL,
            temperature=0.3
        )

        if not raw_response_text:
            print(f"Error: Empty response from OpenRouter verification for {article_url}.")
            # Proceed with URL date parsing as fallback
            final_article_type = "Type unclear (empty response)"
        else:
            print(f"OpenRouter Verification Raw Response for {article_url[:100]}...:\n---\n{raw_response_text}\n---")
            lines = raw_response_text.split('\n')
            # Initialize with defaults that indicate OpenRouter didn't provide this specific field
            results_from_gemini = {
                "publication_date_str": "Date not found by OpenRouter",
                "article_type_assessment": "Type unclear (OpenRouter parsing error)"
            }
            parsed_items_count = 0
            for line in lines[:6]:  # four expected lines, with a little slack for stray blank lines
                line_strip = line.strip()
                if "Publication Date:" in line_strip and results_from_gemini["publication_date_str"] == "Date not found by OpenRouter":
                    results_from_gemini["publication_date_str"] = line_strip.split("Publication Date:", 1)[-1].strip()
                    parsed_items_count += 1
                elif "Article Type:" in line_strip and results_from_gemini["article_type_assessment"] == "Type unclear (OpenRouter parsing error)":
                    results_from_gemini["article_type_assessment"] = line_strip.split("Article Type:", 1)[-1].strip()
                    parsed_items_count += 1
                elif "Sensitive Topic:" in line_strip and sensitive_topic_answer is None:
                    # First word only, so "** Yes." or "No (campus event)" still parse.
                    answer_words = re.findall(r"[a-z]+", line_strip.split("Sensitive Topic:", 1)[-1].lower())
                    if answer_words and answer_words[0] in {"yes", "no"}:
                        sensitive_topic_answer = answer_words[0]
                        parsed_items_count += 1

            gemini_publication_date_str = results_from_gemini["publication_date_str"]
            final_article_type = results_from_gemini["article_type_assessment"]

            if parsed_items_count < 3:
                print(f"Warning: OpenRouter verification response for {article_url} did not parse all expected fields. Parsed: {parsed_items_count}/3.")

    except Exception as e_gemini:
        print(f"Error during OpenRouter API call or parsing for verification of {article_url}: {e_gemini}")
        # Defaults will be used, attempt URL date parsing
        final_article_type = "Type unclear (OpenRouter API error)"

    # Determine final date string (OpenRouter > URL > Not found)
    final_date_str = gemini_publication_date_str
    date_source_log = "(from OpenRouter)"
    if final_date_str.lower() == "date not found" or "error" in final_date_str.lower() or not final_date_str.strip():
        print(f"Info: OpenRouter did not find date for {article_url}. Attempting URL parse.")
        url_extracted_date = publication_date
        if url_extracted_date:
            final_date_str = url_extracted_date
            date_source_log = "(from URL)"
            print(f"Info: Extracted date {final_date_str} from URL for {article_url}.")
        else:
            final_date_str = "Date not found"
            date_source_log = "(no date found)"

    # Determine recency based on the final_date_str and configured date range
    # Allow disabling via env var for temporary bypass during debugging or quota limits
    disable_range_check = os.getenv("DISABLE_DATE_RANGE_CHECK", "").lower() in {"1", "true", "yes"}
    is_recent_status = "Date unclear"
    is_within_range = False  # Track if date is actually within range

    if disable_range_check:
        is_recent_status = "Range check disabled"
        is_within_range = True
    else:
        if final_date_str.lower() == "date not found" or "error" in final_date_str.lower():
            is_recent_status = f"Date unclear {date_source_log}"
        else:
            try:
                publication_date_dt = datetime.strptime(final_date_str, "%Y-%m-%d").date()
                if publication_date_dt >= start_date and publication_date_dt <= end_date:
                    is_recent_status = f"Within range {date_source_log} ({start_date} to {end_date})"
                    is_within_range = True
                elif publication_date_dt > end_date:
                    is_recent_status = f"After range {date_source_log} (after {end_date})"
                else:
                    is_recent_status = f"Before range {date_source_log} (before {start_date})"
            except ValueError:
                is_recent_status = f"Date unparsable {date_source_log}"
                print(f"Warning: Could not parse final date '{final_date_str}' for {article_url}.")

    # Relevance itself was filtered in Step 1; here it only drops sensitive topics.
    # Every caller already skips anything not marked "Relevant", and an unanswered
    # check counts as a skip so a sensitive story can't slip through on a bad reply.
    relevance = "Relevant"
    if config.SENSITIVE_TOPIC_FILTER_ENABLED:
        if sensitive_topic_answer == "yes":
            relevance = "Not relevant (sensitive topic)"
            print(f"Info: Skipping {article_url}: flagged as a sensitive topic.")
        elif sensitive_topic_answer != "no":
            relevance = "Unclear (sensitive-topic check got no answer)"

    final_results = {
        "url": article_url,
        "publication_date_str": final_date_str,
        "is_recent": is_recent_status,
        "is_within_range": is_within_range,
        "is_relevant": relevance,
        "article_type_assessment": final_article_type if 'final_article_type' in locals() else "Type unclear (init error)"
    }
    print(f"Verification results for {article_url[:100]}...: {final_results}")
    return final_results

if __name__ == '__main__':
    print("Testing Article Handler...")
    import sys
    import os
    PROJECT_ROOT_FOR_TEST = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if PROJECT_ROOT_FOR_TEST not in sys.path:
         sys.path.insert(0, PROJECT_ROOT_FOR_TEST)
    
    from news_bot.core import config
    config.validate_config()
    if not hasattr(config, 'OPENROUTER_MODEL_CONTEXT_LIMIT_CHARS'):
        print("Warning: OPENROUTER_MODEL_CONTEXT_LIMIT_CHARS not in config, defaulting to 2000000 for test.")
        config.OPENROUTER_MODEL_CONTEXT_LIMIT_CHARS = 2000000

    # Test URL with date in it
    test_url_wsn_with_date = "https://nyunews.com/news/2024/05/16/gallatin-student-speaker-diploma-withheld/"
    # Test URL likely without clear date in URL, relying on Gemini or byline (if fetched)
    test_url_nyu_no_clear_url_date = "https://www.nyu.edu/about/news-publications/news/2024/march/nyu-appoints-deans-for-arts-and-science-and-global-public-heal.html"
    # Test with a non-article page that might have a date-like structure in URL
    test_url_category_like = "https://nyunews.com/category/news/2023/"

    urls_to_test = [test_url_wsn_with_date, test_url_nyu_no_clear_url_date, test_url_category_like]

    for i, test_url in enumerate(urls_to_test):
        print(f"\n--- Test {i+1}: Processing URL: {test_url} ---")
        
        # Test URL date extraction directly
        # extracted_date = extract_date_from_url(test_url) # Not used in this test
        print(f"  Direct URL date extraction attempt: {extracted_date}")

        article_text = fetch_and_extract_text(test_url)
        if not article_text and "category" in test_url: # For category pages, text extraction might be minimal
            print("  Minimal or no text expected from category page, using placeholder for verification test.")
            article_text = "This is a category page listing many articles about NYU events." 
        
        if article_text:
            verification_results = verify_article_with_gemini(article_text, test_url)
            if verification_results:
                print(f"Verification Results for {test_url}:\n{json.dumps(verification_results, indent=2)}")
            else:
                print(f"Failed to get verification results for {test_url}.")
        else:
            print(f"Failed to fetch or extract text from {test_url} (and not a category page for test text). Skipping verification.")