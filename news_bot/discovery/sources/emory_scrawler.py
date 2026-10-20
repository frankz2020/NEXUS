import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, date, timedelta
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup
from ...discovery.date_extractor import extract_date_from_url, extract_ymd_from_text
from ...core import config, school_config
from ...utils import http_fetcher

school = school_config.SCHOOL_PROFILES['emory']

EMORY_REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}
EMORY_RETRYABLE_STATUS_CODES = {405, 429, 500, 502, 503, 504}
# Statuses that still fail after http_fetcher's browser fallback (and, for 429, after backoff
# retries) mean the Wheel is blocking this server; it answers datacenter clients such as
# Railway with 405.
EMORY_BLOCKED_STATUS_CODES = {403, 405, 429}
# news.emory.edu moved to Drupal in Oct 2026: the monthly /stories/YYYY/MM/ index pages
# now 404, and story URLs are /stories/YYYY/MM/<slug> listed in the sitemap.
EMORY_NEWS_SITEMAP_URL = 'https://news.emory.edu/sitemap.xml'
SITEMAP_NS = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
EMORY_STORY_URL_RE = re.compile(r'^https://news\.emory\.edu/stories/(\d{4})/(\d{2})/[^/?#]+/?$')

def _month_iter(start_date: date, end_date: date):
    y, m = start_date.year, start_date.month
    while True:
        yield y, m
        if y == end_date.year and m == end_date.month:
            break
        m += 1
        if m == 13:
            m = 1
            y += 1


def parse_date_value(raw_value: str | None) -> str | None:
    if not raw_value:
        return None

    raw_value = raw_value.strip()
    if not raw_value:
        return None

    if re.match(r'^\d{4}-\d{2}-\d{2}$', raw_value):
        return raw_value

    try:
        parsed_dt = datetime.fromisoformat(raw_value.replace('Z', '+00:00'))
        return parsed_dt.date().strftime('%Y-%m-%d')
    except ValueError:
        return extract_ymd_from_text(raw_value)


def build_emory_external_page_variants(page_url: str, page_num: int) -> list[str]:
    if page_num == 0:
        return [page_url]

    base = page_url.rstrip('/')
    separator = '&' if '?' in page_url else '?'
    variants = [
        f"{page_url}{separator}page={page_num}",
        f"{page_url}{separator}pageNumber={page_num}",
        f"{base}/page/{page_num}/",
        f"{page_url}{separator}page={page_num + 1}",
        f"{page_url}{separator}pageNumber={page_num + 1}",
        f"{base}/page/{page_num + 1}/",
    ]

    deduped_variants = []
    seen = set()
    for variant in variants:
        if variant not in seen:
            deduped_variants.append(variant)
            seen.add(variant)
    return deduped_variants


def fetch_emory_page(url: str, referer: str | None = None, max_attempts: int = 3) -> requests.Response:
    headers = dict(EMORY_REQUEST_HEADERS)
    if referer:
        headers['Referer'] = referer

    last_response = None
    for attempt in range(1, max_attempts + 1):
        response = requests.get(url, headers=headers, timeout=config.URL_FETCH_TIMEOUT)
        last_response = response

        if response.status_code == 404:
            return response
        if response.status_code not in EMORY_RETRYABLE_STATUS_CODES:
            response.raise_for_status()
            return response

        if attempt == max_attempts:
            response.raise_for_status()

        sleep_seconds = attempt
        print(
            f"  Emory request retry {attempt}/{max_attempts - 1} for {url} "
            f"after status {response.status_code}"
        )
        time.sleep(sleep_seconds)

    assert last_response is not None
    return last_response


def extract_date_from_emory_article_page(article_url: str) -> str | None:
    try:
        resp = fetch_emory_page(article_url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, 'html.parser')

        meta_candidates = [
            ('property', 'article:published_time'),
            ('property', 'og:published_time'),
            ('name', 'publish-date'),
            ('name', 'pubdate'),
            ('name', 'date'),
        ]
        for attr_name, attr_value in meta_candidates:
            meta_tag = soup.find('meta', attrs={attr_name: attr_value})
            if meta_tag and meta_tag.get('content'):
                parsed_date = parse_date_value(meta_tag.get('content'))
                if parsed_date:
                    return parsed_date

        time_tag = soup.find('time')
        if time_tag:
            parsed_date = parse_date_value(time_tag.get('datetime') or time_tag.get_text(strip=True))
            if parsed_date:
                return parsed_date
    except Exception:
        return None

    return None


def is_allowed_domain(article_url: str) -> bool:
    hostname = (urlparse(article_url).hostname or '').lower()
    if not hostname:
        return False

    for domain in school.get('domains', []):
        normalized = domain.lower().strip()
        if hostname == normalized or hostname.endswith(f".{normalized}"):
            return True
    return False


def emory_collect_generic_candidate_links(soup: BeautifulSoup, current_page_url: str) -> dict:
    candidate_links = {}

    for link_tag in soup.find_all('a', href=True):
        href = (link_tag.get('href') or '').strip()
        if not href:
            continue

        abs_url = urljoin(current_page_url, href)
        if not abs_url.startswith("http"):
            continue

        if not is_allowed_domain(abs_url):
            continue

        if any(skip in abs_url.lower() for skip in ["/tag/", "/author/", "/page/", "/category/", "javascript:", "mailto:", "#"]):
            continue

        if not (
            re.search(r'/\d{4}/\d{2}/\d{2}/', abs_url) or
            "/news/" in abs_url or
            "/newsculture-articles/" in abs_url
        ):
            continue

        candidate_links[link_tag] = extract_date_from_url(abs_url)

    return candidate_links


def fetch_emory_wheel_page(url: str, referer: str, force_browser: bool, max_attempts: int = 3):
    """http_fetcher.fetch_page plus the backoff retry on 429/5xx that fetch_emory_page provides."""
    for attempt in range(1, max_attempts + 1):
        try:
            return http_fetcher.fetch_page(
                url,
                timeout=config.URL_FETCH_TIMEOUT,
                referer=referer,
                force_browser=force_browser,
            )
        except requests.exceptions.HTTPError as e:
            status_code = getattr(e.response, 'status_code', None)
            retryable = status_code is not None and (status_code == 429 or status_code >= 500)
            if not retryable or attempt == max_attempts:
                raise
            print(f"  Emory request retry {attempt}/{max_attempts - 1} for {url} after status {status_code}")
            time.sleep(attempt)


def emory_scan_wheel_pages_for_date_range() -> list[dict]:
    start_date, end_date = config.get_news_date_range()
    print(f"\n--- Scanning Archive Pages for {start_date} to {end_date} ---")
    
    found_articles: list[dict] = []
    processed_urls: set[str] = set()

    if not school.get('category_pages'):
        return []

    # Emory Wheel now canonicalizes this feed under /category/news.
    page_url = school['category_pages'][1]
    # Try to scan multiple pages if the site supports pagination
    max_pages = config.MAX_CATEGORY_PAGES_TO_SCAN  # Use config value    
    break_signal = False
    use_browser = False
    
    for page_num in range(1, max_pages):
        if break_signal:
            break
        current_page_url = f"{page_url}?page={page_num}&per_page=20"
        print(f"Checking Emory wheel page: {current_page_url}")
        try:
            fetched_page = fetch_emory_wheel_page(current_page_url, page_url, force_browser=use_browser)
            # Once plain requests has been blocked, skip straight to the browser for later pages.
            use_browser = fetched_page.source == 'playwright'
            soup = BeautifulSoup(fetched_page.content, 'html.parser')
            
            
            for article in soup.find_all('article'):
                article_links = [
                    link for link in article.find_all('a', href=True)
                    if '/article/' in (link.get('href') or '')
                ]
                if not article_links:
                    continue

                a = next(
                    (link for link in article_links if link.get_text(strip=True)),
                    article_links[0],
                )
                href = a['href']
                abs_url = urljoin(current_page_url, href)
                if 'emorywheel.com/article/' not in abs_url:
                    continue
                if abs_url in processed_urls:
                    continue
                
                title = (
                    a.get_text(strip=True)
                    or a.get('title')
                    or a.get('aria-label', '').removeprefix('Read ').strip()
                    or 'Untitled'
                )

                time_tag = article.find('time')
                url_date = parse_date_value(
                    time_tag.get('datetime') or time_tag.get_text(strip=True)
                ) if time_tag else None
                
                # Compare dates only after converting to a date object
                if url_date:
                    try:
                        article_date = datetime.strptime(url_date, '%Y-%m-%d').date()
                        if article_date < start_date:
                            break_signal = True                       
                        if article_date < start_date or article_date > end_date:
                            continue
                    except ValueError:
                        # If parsing fails, keep the article without filtering by date
                        pass       
                
                found_articles.append({
                    'title': title,
                    'url': abs_url,
                    'snippet': title,
                    'url_date': url_date
                })
                processed_urls.add(abs_url)
                
        except requests.exceptions.RequestException as e:
            print(f"  Error accessing Emory wheel page {current_page_url}: {e}")
            status_code = getattr(getattr(e, 'response', None), 'status_code', None)
            if status_code in EMORY_BLOCKED_STATUS_CODES:
                # Every later page is blocked the same way, so don't retry them one by one.
                print("  Emory Wheel is blocking this server even via browser; skipping remaining pages")
                break
        except Exception as e:
            print(f"  Error processing Emory wheel page {current_page_url}: {e}")

    print(f"Emory wheel pages yielded {len(found_articles)} articles in range")
    return found_articles



def fetch_emory_story_meta(article_url: str) -> tuple[str, str | None]:
    """Read a story's title and publish date, which the sitemap doesn't carry.

    Story pages have no date metadata; the publish date ("October 9, 2026") sits in a
    <strong> next to the <h1> inside the page's <hgroup>.
    """
    slug = article_url.rstrip('/').rsplit('/', 1)[-1]
    title, publish_date = slug.replace('-', ' ').capitalize(), None

    resp = fetch_emory_page(article_url)
    resp.raise_for_status()  # fetch_emory_page returns 404s without raising
    soup = BeautifulSoup(resp.content, 'html.parser')

    h1 = soup.find('h1')
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(strip=True)
    header = h1.find_parent('hgroup') if h1 else None
    if header:
        for strong in header.find_all('strong'):
            publish_date = extract_ymd_from_text(strong.get_text(strip=True))
            if publish_date:
                break
    return title, publish_date


def fetch_emory_sitemap(url: str) -> ET.Element:
    resp = fetch_emory_page(url)
    resp.raise_for_status()  # fetch_emory_page returns 404s without raising
    return ET.fromstring(resp.content)


def emory_scan_edu_pages_for_date_range() -> list[dict]:
    start_date, end_date = config.get_news_date_range()
    print(f"\n--- Scanning Emory News sitemap for {start_date} to {end_date} ---")

    found_articles: list[dict] = []
    processed_urls: set[str] = set()
    target_months = set(_month_iter(start_date, end_date))

    try:
        index_root = fetch_emory_sitemap(EMORY_NEWS_SITEMAP_URL)
        sitemap_pages = [
            loc.text.strip()
            for loc in index_root.findall('sm:sitemap/sm:loc', SITEMAP_NS)
            if loc.text
        ]
    except (requests.exceptions.RequestException, ET.ParseError) as e:
        print(f"  Error accessing Emory sitemap {EMORY_NEWS_SITEMAP_URL}: {e}")
        return []

    # Sitemap pages follow the Drupal migration order, not publish date (2025 stories sit on
    # page 1, 2026 ones on the last page), so every page has to be checked.
    for sitemap_page in sitemap_pages:
        print(f"Checking Emory sitemap page: {sitemap_page}")
        try:
            page_root = fetch_emory_sitemap(sitemap_page)
        except (requests.exceptions.RequestException, ET.ParseError) as e:
            print(f"  Error accessing Emory sitemap page {sitemap_page}: {e}")
            continue

        for entry in page_root.findall('sm:url', SITEMAP_NS):
            loc = entry.findtext('sm:loc', default='', namespaces=SITEMAP_NS)
            match = EMORY_STORY_URL_RE.match(loc.strip())
            if not match:
                continue
            if (int(match.group(1)), int(match.group(2))) not in target_months:
                continue

            abs_url = match.group(0)
            if abs_url in processed_urls:
                continue

            try:
                # lastmod is the last edit, never earlier than publication, so a story last
                # touched before the range cannot have been published inside it.
                lastmod_date = parse_date_value(
                    entry.findtext('sm:lastmod', default=None, namespaces=SITEMAP_NS)
                )
                if lastmod_date and datetime.strptime(lastmod_date, '%Y-%m-%d').date() < start_date:
                    continue

                title, url_date = fetch_emory_story_meta(abs_url)
                url_date = url_date or lastmod_date
                if url_date:
                    article_date = datetime.strptime(url_date, '%Y-%m-%d').date()
                    if article_date < start_date or article_date > end_date:
                        continue

                found_articles.append({
                    'title': title,
                    'url': abs_url,
                    'snippet': title,
                    'url_date': url_date
                })
                processed_urls.add(abs_url)
            except Exception as e:
                print(f"  Error processing Emory story {abs_url}: {e}")

    print(f"Emory News sitemap yielded {len(found_articles)} articles in range")
    return found_articles


def emory_scan_external_source_pages_for_date_range() -> list[dict]:
    start_date, end_date = config.get_news_date_range()
    print(f"\n--- Scanning Emory external source pages for {start_date} to {end_date} ---")

    found_articles: list[dict] = []
    processed_urls: set[str] = set()
    external_pages = school.get('external_category_pages', [])

    if not external_pages:
        print("  Info: No Emory external source pages configured.")
        return []

    for page_url in external_pages:
        max_pages = config.MAX_CATEGORY_PAGES_TO_SCAN
        for page_num in range(max_pages + 1):
            resp = None
            current_page_url = None
            for page_variant in build_emory_external_page_variants(page_url, page_num):
                try:
                    candidate_resp = fetch_emory_page(page_variant, referer=page_url)
                    if candidate_resp.status_code == 404:
                        continue
                    candidate_resp.raise_for_status()
                    resp = candidate_resp
                    current_page_url = candidate_resp.url
                    break
                except requests.exceptions.RequestException:
                    continue

            if resp is None or current_page_url is None:
                if page_num == 0:
                    print(f"  External source page not found: {page_url}")
                break

            try:
                soup = BeautifulSoup(resp.content, 'html.parser')
                candidate_links = emory_collect_generic_candidate_links(soup, current_page_url)

                if not candidate_links:
                    print(f"  Info: No candidate article links found on external source page: {current_page_url}")
                    if page_num > 0:
                        break
                    continue

                for link_tag, url_date in candidate_links.items():
                    href = link_tag.get('href')
                    abs_url = urljoin(current_page_url, href)
                    if abs_url in processed_urls:
                        continue

                    title = link_tag.get_text(strip=True) or link_tag.get('title') or ''
                    if not title:
                        continue

                    if url_date is None:
                        url_date = extract_date_from_emory_article_page(abs_url)

                    if url_date is None:
                        if config.NEWS_START_DATE:
                            continue
                        found_articles.append({
                            'title': title,
                            'url': abs_url,
                            'snippet': title,
                            'url_date': None
                        })
                        processed_urls.add(abs_url)
                        continue

                    try:
                        article_date = datetime.strptime(url_date, "%Y-%m-%d").date()
                    except ValueError:
                        continue

                    if article_date < start_date or article_date > end_date:
                        continue

                    found_articles.append({
                        'title': title,
                        'url': abs_url,
                        'snippet': title,
                        'url_date': url_date
                    })
                    processed_urls.add(abs_url)

            except Exception as e:
                print(f"  Error processing Emory external source page {current_page_url}: {e}")

    print(f"Emory external source pages yielded {len(found_articles)} articles in range")
    return found_articles



def emory_scan_archive_pages_for_date_range() -> list[dict]:
    edu_articles = emory_scan_edu_pages_for_date_range()
    wheel_articles = emory_scan_wheel_pages_for_date_range()
    external_articles = emory_scan_external_source_pages_for_date_range()
    return edu_articles + wheel_articles + external_articles