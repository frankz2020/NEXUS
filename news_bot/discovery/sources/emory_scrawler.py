import re
from datetime import datetime, date, timedelta
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup
from ...discovery.date_extractor import extract_date_from_url, extract_ymd_from_text
from ...core import config, school_config

school = school_config.SCHOOL_PROFILES['emory']

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


def extract_date_from_emory_article_page(article_url: str) -> str | None:
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        resp = requests.get(article_url, headers=headers, timeout=config.URL_FETCH_TIMEOUT)
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


def emory_scan_wheel_pages_for_date_range() -> list[dict]:
    start_date, end_date = config.get_news_date_range()
    print(f"\n--- Scanning Archive Pages for {start_date} to {end_date} ---")
    
    found_articles: list[dict] = []
    processed_urls: set[str] = set()

    if not school.get('category_pages'):
        return []

    # Emory uses a monthly index page
    page_url = school['category_pages'][1]  # https://www.emorywheel.com/section/news?page=1&per_page=20
    # Try to scan multiple pages if the site supports pagination
    max_pages = config.MAX_CATEGORY_PAGES_TO_SCAN  # Use config value    
    break_signal = False
    
    for page_num in range(1, max_pages):
        if break_signal:
            break
        current_page_url = f"{page_url}?page={page_num}&per_page=20"
        print(f"Checking Emory wheel page: {current_page_url}")
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            resp = requests.get(current_page_url, headers=headers, timeout=config.URL_FETCH_TIMEOUT)
            if resp.status_code == 404:
                print(f"  Page not found: {current_page_url}")
                continue
            resp.raise_for_status()
            soup = BeautifulSoup(resp.content, 'html.parser')
            
            
            for article in soup.find_all('article'):
                a = article.find_all('a', href=True)[1]
                href = a['href']
                abs_url = urljoin(current_page_url, href)
                if 'emorywheel.com/article/' not in abs_url:
                    continue
                if abs_url in processed_urls:
                    continue
                
                title = a['title'] or 'Untitled'
                url_date = article.find_all('span', class_='dateline')[1].get_text(strip=True)
                url_date = extract_ymd_from_text(url_date)
                
                # Compare dates only after converting to a date object
                if url_date:
                    try:
                        article_date = datetime.strptime(url_date, '%Y-%m-%d').date()
                        if article_date < start_date:
                            break_signal = True                       
                        if article_date < start_date or article_date > end_date:
                            print(start_date, url_date, end_date)
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
        except Exception as e:
            print(f"  Error processing Emory wheel page {current_page_url}: {e}")

    print(f"Emory wheel pages yielded {len(found_articles)} articles in range")
    return found_articles



def emory_scan_edu_pages_for_date_range() -> list[dict]:
    start_date, end_date = config.get_news_date_range()
    print(f"\n--- Scanning Archive Pages for {start_date} to {end_date} ---")
    
    found_articles: list[dict] = []
    processed_urls: set[str] = set()

    if not school.get('archive_patterns'):
        return []

    # Emory uses a monthly index page
    monthly_pattern = school['archive_patterns'][0]  # https://news.emory.edu/stories/{year}/{month:02d}/index.html

    for y, m in _month_iter(start_date, end_date):
        # get the archive url for the month
        archive_url = monthly_pattern.format(year=y, month=m)
        print(f"Checking Emory monthly archive: {archive_url}")
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            resp = requests.get(archive_url, headers=headers, timeout=config.URL_FETCH_TIMEOUT)
            if resp.status_code == 404:
                print(f"  Archive not found: {archive_url}")
                continue
            resp.raise_for_status()
            soup = BeautifulSoup(resp.content, 'html.parser')

            # Find story links on the monthly index
            for a in soup.find_all('a', href=True):
                href = a['href']
                abs_url = urljoin(archive_url, href)
                if 'news.emory.edu/stories/' not in abs_url:
                    continue
                if not abs_url.endswith('/story.html'):
                    continue
                if abs_url in processed_urls:
                    continue

                title = a.get_text(strip=True) or 'Untitled'

                # Try to extract date from slug; if missing, approximate to month start
                url_date = extract_date_from_url(abs_url)
                # If date is not found, fetch the date from child element(<div class="tag-list-item-meta">) of <a href="...">
                if url_date is None:
                    meta = a.find('div', class_='tag-list-item-meta')
                    if meta:
                        url_date = meta.get_text(strip=True)

                try:
                    ad = datetime.strptime(url_date, '%Y-%m-%d').date()
                    if ad < start_date or ad > end_date:
                        continue
                except ValueError:
                    pass

                found_articles.append({
                    'title': title,
                    'url': abs_url,
                    'snippet': title,
                    'url_date': url_date
                })
                processed_urls.add(abs_url)

        except requests.exceptions.RequestException as e:
            print(f"  Error accessing Emory archive {archive_url}: {e}")
        except Exception as e:
            print(f"  Error processing Emory archive {archive_url}: {e}")

    print(f"Emory monthly archives yielded {len(found_articles)} articles in range")
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
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                    }
                    candidate_resp = requests.get(page_variant, headers=headers, timeout=config.URL_FETCH_TIMEOUT)
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