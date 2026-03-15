import os
import json # For potential direct JSON parsing if needed, though client library handles most
from datetime import date, timedelta, datetime
from googleapiclient.discovery import build # For Google Custom Search API
from ...core import config, school_config
from ...discovery.date_extractor import extract_date_from_url
import requests # For fetching category pages
from bs4 import BeautifulSoup # For parsing category pages
from urllib.parse import urljoin, urlparse # For resolving relative URLs
import re # For regular expressions
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime


def build_ucd_page_url(page_url: str, page_num: int) -> str:
    if page_num == 0:
        return page_url
    if "www.ucdavis.edu/news/latest" in page_url:
        if "?" in page_url:
            return f"{page_url}&page={page_num}"
        return f"{page_url}/?page={page_num}"
    if "theaggie.org" in page_url:
        base = page_url.rstrip("/") + "/"
        return f"{base}page/{page_num}/"
    separator = "&" if "?" in page_url else "?"
    return f"{page_url}{separator}page={page_num}"


def is_allowed_domain(article_url: str, school: dict) -> bool:
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
    school = school_config.SCHOOL_PROFILES['ucd']
    skip_patterns = ["/tag/", "/author/", "/page/", "/category/", "javascript:", "mailto:", "#"]

    for link_tag in soup.find_all('a', href=True):
        href = (link_tag.get('href') or '').strip()
        if not href:
            continue

        absolute_url = urljoin(current_page_url, href)
        if not absolute_url.startswith("http"):
            continue

        if not is_allowed_domain(absolute_url, school):
            continue

        if any(skip in absolute_url.lower() for skip in skip_patterns):
            continue

        if not (
            re.search(r'/\d{4}/\d{2}/\d{2}/', absolute_url) or
            "/news/" in absolute_url or
            "/article/" in absolute_url
        ):
            continue

        candidate_links[link_tag] = extract_date_from_url(absolute_url)

    return candidate_links


def parse_rss_date(raw_value: str | None) -> str | None:
    if not raw_value:
        return None

    try:
        return parsedate_to_datetime(raw_value).date().strftime('%Y-%m-%d')
    except Exception:
        return None


def ucd_scan_leadership_rss_for_links() -> list[dict[str, str | None]]:
    start_date, end_date = config.get_news_date_range()
    rss_url = "https://leadership.ucdavis.edu/news.rss"
    print(f"\n--- Scanning UC Davis leadership RSS for {start_date} to {end_date} ---")

    found_articles = []
    processed_urls = set()

    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8',
        }
        response = requests.get(rss_url, headers=headers, timeout=config.URL_FETCH_TIMEOUT)
        response.raise_for_status()
        root = ET.fromstring(response.content)

        for item in root.findall('.//item'):
            title = (item.findtext('title') or '').strip()
            link = (item.findtext('link') or '').strip()
            if not link or link in processed_urls:
                continue

            url_date = parse_rss_date(item.findtext('pubDate')) or extract_date_from_url(link)
            if url_date:
                try:
                    article_date = datetime.strptime(url_date, "%Y-%m-%d").date()
                    if article_date < start_date or article_date > end_date:
                        continue
                except ValueError:
                    continue
            elif config.NEWS_START_DATE:
                continue

            found_articles.append({
                "title": title or link,
                "url": link,
                "snippet": title or link,
                "url_date": url_date,
            })
            processed_urls.add(link)
    except Exception as e:
        print(f"Error fetching UC Davis leadership RSS {rss_url}: {e}")

    return found_articles


def ucd_enterprise_news_pages_for_links() -> list[dict]:
    start_date, end_date = config.get_news_date_range()
    print(f"\n--- Scanning Enterprise News Pages for {start_date} to {end_date} ---")
    found_articles = []
    processed_urls = set()
    
    urls = [
        "https://www.davisenterprise.com/news/crime_fire_courts/",
        "https://www.davisenterprise.com/news/city_government/",
        "https://www.davisenterprise.com/news/state_government/",
    ]
    for url in urls:
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            response = requests.get(url, headers=headers, timeout=config.URL_FETCH_TIMEOUT)
                
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')

            articles = soup.find_all('article')
            for article in articles:
                # 选取标题链接（尽量使用稳定类名，兜底任何 a[href]）
                a = (article.select_one('a.tnt-asset-link[href]') or
                     article.select_one('a.item__anchor[href]') or
                     article.find('a', href=True))
                if not a:
                    continue
                title = (a.get_text(strip=True) or a.get('aria-label') or a.get('title') or '').strip()
                href = (a.get('href') or '').strip()
                if not href:
                    continue
                abs_url = urljoin(response.url, href)
                if abs_url in processed_urls:
                    continue

                # 提取日期：优先 <time datetime="...">，否则从 URL 兜底
                article_date = None
                url_date_str = None
                time_tag = article.select_one('time[datetime]')
                if time_tag and time_tag.has_attr('datetime'):
                    try:
                        dt = datetime.fromisoformat(time_tag['datetime'].replace('Z', '+00:00'))
                        article_date = dt.date()
                        url_date_str = article_date.strftime('%Y-%m-%d')
                    except Exception:
                        article_date = None
                        url_date_str = None
                if article_date is None:
                    extracted = extract_date_from_url(abs_url)
                    if extracted:
                        url_date_str = extracted
                        try:
                            article_date = datetime.strptime(extracted, "%Y-%m-%d").date()
                        except ValueError:
                            article_date = None

                # 按窗口过滤（仅当我们获得了 article_date 才过滤）
                if article_date:
                    if article_date < start_date or article_date > end_date:
                        continue

                found_articles.append({
                    "title": title or "Untitled",
                    "url": abs_url,
                    "snippet": title or "Untitled",
                    "url_date": url_date_str
                })
                processed_urls.add(abs_url)
        except Exception as e:
            print(f"Error fetching UC Davis Enterprise news page {url}: {e}")
            continue
        
    return found_articles


def ucd_scan_category_pages_for_links() -> list[dict[str, str]]:
    """
    Scans configured category pages for direct links to articles.
    Attempts to scan multiple pages to find articles within the configured date range.
    Returns a list of dictionaries, each containing 'title', 'url', and 'snippet' (title used as snippet).
    """
    found_articles = []
    processed_urls = set()
    school = school_config.SCHOOL_PROFILES['ucd']
    leadership_articles = ucd_scan_leadership_rss_for_links()
    found_articles.extend(leadership_articles)
    processed_urls.update(article["url"] for article in leadership_articles)
    configured_page_groups = [
        ("category", school.get('category_pages', [])),
        ("external", school.get('external_category_pages', [])),
    ]

    # Get the configured date range for filtering
    start_date, end_date = config.get_news_date_range()
    print(f"\n--- Scanning Category Pages for Article Links (targeting {start_date} to {end_date}) ---")
    
    for page_group_name, page_urls in configured_page_groups:
        if not page_urls:
            print(f"Info: No UCD {page_group_name} pages configured.")
            continue

        for page_url in page_urls:
            # Try to scan multiple pages if the site supports pagination
            max_pages = config.MAX_CATEGORY_PAGES_TO_SCAN  # Use config value
            for page_num in range(max_pages + 1):
                current_page_url = build_ucd_page_url(page_url, page_num)
                print(f"Scanning {page_group_name} page {page_num}: {current_page_url}")
                
                try:
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                    }
                    response = requests.get(current_page_url, headers=headers, timeout=config.URL_FETCH_TIMEOUT)
                    
                    # If pagination doesn't exist, break the loop
                    if page_num > 1 and response.status_code == 404:
                        print(f"  Page {page_num} not found, stopping pagination.")
                        break
                        
                    response.raise_for_status()
                    soup = BeautifulSoup(response.content, 'html.parser')

                    candidate_links = {}
                    
                    # Method 1: Find links within heading tags inside class "vm-teaser__body" 
                    teaser_body = soup.find_all('div', class_='vm-teaser__body')
                    for body in teaser_body:
                        heading_element = body.find('h3')
                        if heading_element:
                            link_tag = heading_element.find('a', href=True)
                            if link_tag and link_tag not in candidate_links:
                                # Quick pre-filter for news URLs
                                href = link_tag.get('href', '')
                                if '/news/' in href or re.search(r'/\d{4}/\d{2}/', href):
                                    # Extract date from text inside the article
                                    time_tag = body.find('time')
                                    url_date = None
                                    if time_tag and time_tag.get('datetime'):
                                        try:
                                            url_date = datetime.fromisoformat(
                                                time_tag.get('datetime').replace('Z', '+00:00')
                                            ).date().strftime('%Y-%m-%d')
                                        except ValueError:
                                            url_date = None
                                    if not url_date:
                                        url_date = extract_date_from_url(urljoin(current_page_url, href))
                                    candidate_links[link_tag] = url_date

                    # Method 2: Generic fallback for The Aggie and leadership pages.
                    if not candidate_links:
                        candidate_links = collect_generic_candidate_links(soup, current_page_url)
                                   
                    if not candidate_links:
                        print(f"  Info: No candidate article links found on page {page_num}.")
                        continue

                    print(f"  Found {len(candidate_links)} candidate links on page {page_num}")

                    articles_found_on_page = 0
                    filtered_count = {"no_title": 0, "duplicate": 0, "wrong_domain": 0, "bad_pattern": 0, "out_of_range": 0}
                    
                    for link_tag, url_date in candidate_links.items():
                        raw_url = link_tag['href']
                        title = link_tag.get_text(strip=True)
                        absolute_url = urljoin(current_page_url, raw_url)

                        if not title:
                            filtered_count["no_title"] += 1
                            continue
                            
                        if absolute_url in processed_urls:
                            filtered_count["duplicate"] += 1
                            continue

                        # Validate URL structure and domain
                        if not absolute_url.startswith("http") or not is_allowed_domain(absolute_url, school):
                            filtered_count["wrong_domain"] += 1
                            continue

                        # Filter out common non-article paths (expanded list)
                        skip_patterns = [
                            "/category/", "/tag/", "/author/", "/page/",
                            "/staff_name/", "/staff/", "/writer/", "/contributor/",
                            "/about/", "/contact/", "/privacy/", "/terms/",
                            "/subscribe/", "/newsletter/", "/membership/",
                            "/search/", "/archive/", "/topic/",
                            "#", "javascript:", "mailto:"
                        ]
                        if any(skip_path in absolute_url.lower() for skip_path in skip_patterns):
                            filtered_count["bad_pattern"] += 1
                            continue
                                           
                        # For valid articles, check if they're in our date range
                        if url_date:
                            try:
                                article_date = datetime.strptime(url_date, "%Y-%m-%d").date()
                                if article_date < start_date:
                                    filtered_count["out_of_range"] += 1
                                    continue
                                elif article_date > end_date:
                                    filtered_count["out_of_range"] += 1
                                    continue
                                else:
                                    print(f"    ✓ Found article in target range: '{title[:50]}...' ({url_date})")
                                    found_articles.append({"title": title, "url": absolute_url, "snippet": title, "url_date": url_date})
                                    processed_urls.add(absolute_url)
                                    articles_found_on_page += 1
                            except ValueError:
                                found_articles.append({"title": title, "url": absolute_url, "snippet": title, "url_date": None})
                                processed_urls.add(absolute_url)
                                articles_found_on_page += 1
                        else:
                            # No date in URL - skip for historical searches unless it's a special case
                            if config.NEWS_START_DATE:  # If we're doing a historical search
                                filtered_count["bad_pattern"] += 1
                                continue
                            else:
                                # For current news, include articles without dates for verification
                                found_articles.append({"title": title, "url": absolute_url, "snippet": title, "url_date": url_date})
                                processed_urls.add(absolute_url)
                                articles_found_on_page += 1
                        
                        if len(found_articles) >= config.MAX_SEARCH_RESULTS_TO_PROCESS * 3:  # Allow more articles for date filtering
                            break
                    
                    print(f"  Page {page_num} results: {articles_found_on_page} kept, filtered: {sum(filtered_count.values())} total")
                    if sum(filtered_count.values()) > 0:
                        print(f"    Filtered: no_title={filtered_count['no_title']}, duplicate={filtered_count['duplicate']}, " +
                              f"wrong_domain={filtered_count['wrong_domain']}, bad_pattern={filtered_count['bad_pattern']}, " +
                              f"out_of_range={filtered_count['out_of_range']}")
                    
                    # If we've found enough articles or no articles on this page, stop pagination
                    if len(found_articles) >= config.MAX_SEARCH_RESULTS_TO_PROCESS * 3 or articles_found_on_page == 0:
                        break
                        
                except requests.exceptions.RequestException as e_req:
                    print(f"Error fetching category page {current_page_url}: {e_req}")
                    if page_num == 0:
                        break  # If first page fails, don't try pagination
                except Exception as e_general:
                    print(f"Error processing category page {current_page_url}: {e_general}")
    
    print(f"Found {len(found_articles)} unique potential articles from category page scans.")
    
    # Sort dated articles first (newest first), then undated ones.
    found_articles.sort(
        key=lambda x: (x.get('url_date') is not None, x.get('url_date') or ''),
        reverse=True,
    )
    found_articles.extend(ucd_enterprise_news_pages_for_links()[:7])
    
    return found_articles