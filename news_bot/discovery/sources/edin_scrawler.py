import re
from datetime import datetime, date, timedelta
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup
from ...discovery.date_extractor import extract_ymd_from_text, extract_date_from_url
from ...core import config, school_config

school = school_config.SCHOOL_PROFILES['edin']


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


def build_edin_external_page_variants(page_url: str, page_num: int) -> list[str]:
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


def extract_date_from_edin_article_page(article_url: str) -> str | None:
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(article_url, headers=headers, timeout=config.URL_FETCH_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')

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


def extract_title_from_edin_article_page(article_url: str) -> str | None:
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(article_url, headers=headers, timeout=config.URL_FETCH_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')

        meta_title = soup.find('meta', attrs={'property': 'og:title'})
        if meta_title and meta_title.get('content'):
            return meta_title.get('content').strip()

        heading = soup.find('h1')
        if heading:
            title = heading.get_text(strip=True)
            if title:
                return title

        if soup.title and soup.title.string:
            return soup.title.string.strip()
    except Exception:
        return None

    return None


def is_edin_relevant_external_article(article_url: str, title: str) -> bool:
    lower_title = (title or '').lower()
    lower_url = article_url.lower()
    combined = f"{lower_title} {lower_url}"

    if any(bad_marker in combined for bad_marker in ['sponsored article', 'sponsored articles', '/viral-news/']):
        return False

    if 'thetab.com' in lower_url:
        return 'edinburgh' in combined or 'heriot-watt' in combined or 'napier' in combined

    if 'expressandstar.com' in lower_url:
        return 'edinburgh' in combined or 'university of edinburgh' in combined

    if 'projectscot.com' in lower_url:
        return 'edinburgh' in combined or 'student' in combined or 'university' in combined

    if 'midlothianview.com' in lower_url:
        return 'edinburgh' in combined or 'university' in combined or 'student' in combined

    if 'oikotimene.org' in lower_url:
        return 'edinburgh' in combined or 'student' in combined or 'wcc' in combined

    return True


def is_allowed_domain(article_url: str) -> bool:
    hostname = (urlparse(article_url).hostname or '').lower()
    if not hostname:
        return False

    for domain in school.get('domains', []):
        normalized = domain.lower().strip()
        if hostname == normalized or hostname.endswith(f".{normalized}"):
            return True
    return False


def collect_generic_candidate_links(soup: BeautifulSoup, current_page_url: str) -> dict:
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
            "/uk-news/" in abs_url
        ):
            continue

        candidate_links[link_tag] = extract_date_from_url(abs_url)

    return candidate_links

def edin_scan_edinburgh_news_pages_for_date_range() -> list[dict]:
    start_date, end_date = config.get_news_date_range()
    print(f"\n--- Scanning Category Pages for {start_date} to {end_date} ---")
    
    found_articles: list[dict] = []
    processed_urls: set[str] = set()
    
    if not school.get('category_pages'):
        return []
    
    # Official Edinburgh News Page with date range specified in the url
    page_url = school['category_pages'][0].format(
        start_year=start_date.year, start_month=start_date.month, start_day=start_date.day,
        end_year=end_date.year,   end_month=end_date.month,   end_day=end_date.day
    )
    print(f"  Checking Edinburgh category page: {page_url}")
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        resp = requests.get(page_url, headers=headers, timeout=config.URL_FETCH_TIMEOUT)
        if resp.status_code == 404:
            print(f"  Archive not found: {page_url}")
            return []
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, 'html.parser')
        cards = soup.find_all('div', class_='news-listing')
        for card in cards:
            a = card.find('a', href=True)
            href = a['href']
            abs_url = urljoin(page_url, href)
            title = a.get_text(strip=True) or 'Untitled'
            card_date = card.find('span', class_='news-date').get_text(strip=True)
            url_date = extract_ymd_from_text(card_date)
            if url_date is None:
                continue
            article_date = datetime.strptime(url_date, "%Y-%m-%d").date()
            if article_date >= start_date and article_date <= end_date:
                found_articles.append({
                    "url": abs_url,
                    "title": title,
                    "snippet": title,
                    "url_date": url_date
                })
                processed_urls.add(abs_url)
    except Exception as e:
        print(f"  Error accessing Edinburgh category page {page_url}: {e}")
    return found_articles

def edin_scan_thestudent_news_pages_for_date_range() -> list[dict]:
    start_date, end_date = config.get_news_date_range()
    print(f"\n--- Scanning The Student News Pages for {start_date} to {end_date} ---")
    
    found_articles: list[dict] = []
    processed_urls: set[str] = set()
    
    page_url = school['category_pages'][1]
    page_count = 0
    max_pages = 30
    flag = True
    
    try:
        # the first page is static, so we don't need to fetch it by AJAX
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(page_url, headers=headers, timeout=config.URL_FETCH_TIMEOUT)
        
        if response.status_code == 404:
            print(f"  Archive page not found: {page_url}")
            return []
        
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        
        content_root = soup.find('div', class_='zeen-col--wide') or soup
        for link in content_root.find_all('a', href=True):
            href = link['href']
            abs_url = urljoin(page_url, href)
            
            if abs_url in processed_urls:
                continue
            # Filter out non-article URLs
            skip_patterns = [
                "/staff_name/", "/staff/", "/writer/", "/contributor/",
                "/category/news/", "/tag/", "/author/", "/page/",
                "/about/", "/contact/", "/privacy/", "/terms/",
                "#", "javascript:", "mailto:"
            ]
            if any(skip_path in abs_url.lower() for skip_path in skip_patterns):
                continue
                                
            title = link.get_text(strip=True) or link.get('title') or ''
            if not title:
                title = extract_title_from_edin_article_page(abs_url) or ''
            if not title:
                continue
            
            url_date = extract_date_from_url(abs_url)
            if url_date is None:
                url_date = extract_date_from_edin_article_page(abs_url)
                if url_date is None:
                    continue
            
            article_date = datetime.strptime(url_date, "%Y-%m-%d").date()
            if article_date >= start_date and article_date <= end_date:
                found_articles.append({
                    "url": abs_url,
                    "title": title,
                    "snippet": title,
                    "url_date": url_date
                })
                processed_urls.add(abs_url)
                
    except Exception as e:
        print(f"  Error accessing The Student News page {page_url}: {e}")
         
    return found_articles


def edin_scan_external_source_pages_for_date_range() -> list[dict]:
    start_date, end_date = config.get_news_date_range()
    print(f"\n--- Scanning Edinburgh external source pages for {start_date} to {end_date} ---")

    found_articles: list[dict] = []
    processed_urls: set[str] = set()
    external_pages = school.get('external_category_pages', [])

    if not external_pages:
        print("  Info: No Edinburgh external source pages configured.")
        return []

    for page_url in external_pages:
        max_pages = config.MAX_CATEGORY_PAGES_TO_SCAN
        for page_num in range(max_pages + 1):
            response = None
            current_page_url = None
            for page_variant in build_edin_external_page_variants(page_url, page_num):
                try:
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                    }
                    candidate_response = requests.get(page_variant, headers=headers, timeout=config.URL_FETCH_TIMEOUT)
                    if candidate_response.status_code == 404:
                        continue
                    candidate_response.raise_for_status()
                    response = candidate_response
                    current_page_url = candidate_response.url
                    break
                except requests.exceptions.RequestException:
                    continue

            if response is None or current_page_url is None:
                if page_num == 0:
                    print(f"  External source page not found: {page_url}")
                break

            try:
                soup = BeautifulSoup(response.content, 'html.parser')
                candidate_links = collect_generic_candidate_links(soup, current_page_url)

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
                        title = extract_title_from_edin_article_page(abs_url) or ''
                    if not title:
                        continue

                    if not is_edin_relevant_external_article(abs_url, title):
                        continue

                    if url_date is None:
                        url_date = extract_date_from_edin_article_page(abs_url)

                    if url_date is None:
                        if config.NEWS_START_DATE:
                            continue
                        found_articles.append({
                            "url": abs_url,
                            "title": title,
                            "snippet": title,
                            "url_date": None
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
                        "url": abs_url,
                        "title": title,
                        "snippet": title,
                        "url_date": url_date
                    })
                    processed_urls.add(abs_url)
            except Exception as e:
                print(f"  Error accessing Edinburgh external source page {current_page_url}: {e}")

    return found_articles

    
def edin_scan_category_pages_for_date_range() -> list[dict]:
    return (
        edin_scan_edinburgh_news_pages_for_date_range()
        + edin_scan_thestudent_news_pages_for_date_range()
        + edin_scan_external_source_pages_for_date_range()
    )