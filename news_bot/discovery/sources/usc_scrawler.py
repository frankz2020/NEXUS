from datetime import date, timedelta, datetime
from googleapiclient.discovery import build # For Google Custom Search API
from ...core import config, school_config
from ...discovery.date_extractor import extract_date_from_url, extract_ymd_from_text
from ...utils import prompt_logger, openrouter_client
import requests # For fetching category pages
from bs4 import BeautifulSoup # For parsing category pages
from urllib.parse import urljoin # For resolving relative URLs
import re # For regular expressions
import json


# def gemini_verify_article(title: str, description: str) -> str:
#     """ 
#     Due to the high volumn of articles from USC source, we need to verify the article with OpenRouter by title and description before proceeding
#     Returns "Relevant" or "Irrelevant"
#     """
#     if not config.OPENROUTER_API_KEY:
#         return "Irrelevant"
    
#     prompt = f"""
#     You are a news analyst. Please analyze the following news article by title and description. 
#     Is this article generally relevant to students at USC, covering campus news, academic updates, student life, or significant events affecting the USC community? return either "Irrelevant" or "Relevant"
#     [some examples of irrelevant articles] If the article is about actor, actress, music star, music, sports star, sports events, sports matches, American/Latin culture study, American Science break through, return "Irrelevant".
#     News Title: {title}
#     News Description: {description}
#     Return either "Relevant" or "Irrelevant".
#     """
    
#     # Log the prompt
#     prompt_logger.log_prompt(
#         "gemini_verify_article",
#         prompt,
#         context={"title": title, "description": description[:200] if len(description) > 200 else description}
#     )
    
#     try:
#         text = openrouter_client.generate_content(
#             prompt=prompt,
#             model=config.GEMINI_FLASH_MODEL,
#             temperature=0.3
#         )
#         if text:
#             return text.strip()
#         return "Irrelevant"
#     except Exception as e:
#         print(f"Error in gemini_verify_article: {e}")
#         return "Irrelevant"


def usc_scan_annenberg_media_for_links() -> list[dict]:
    """
    Scan USC Annenberg Media's Arc XP feed and return in-range story URLs.
    """
    school = school_config.SCHOOL_PROFILES["usc"]
    start_date, end_date = config.get_news_date_range()
    print(f"\n--- Scanning USC Annenberg Media for {start_date} to {end_date} ---")

    base_api = school["category_pages"][0]
    found_articles: list[dict] = []
    processed_urls: set[str] = set()

    should_continue = True
    offset = 0
    size = 40
    max_pages = 30
    page_count = 0
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/91.0.4472.124 Safari/537.36"
        )
    }

    while should_continue:
        if page_count >= max_pages:
            print(f"  Reached safety page limit ({max_pages}); stopping.")
            break

        print(f"  USC API request at offset={offset}, size={size} (page {page_count + 1})")
        try:
            query_obj = {"feature": "results-list", "offset": offset, "size": size}
            filter_str = (
                "content_elements{display_date,headlines{basic},description{basic},type,"
                "websites{uscannenberg{website_url}}},count,next"
            )
            params = {
                "query": json.dumps(query_obj, separators=(",", ":")),
                "filter": filter_str,
                "_website": "uscannenberg",
                "d": "101",
            }
            resp = requests.get(base_api, params=params, headers=headers, timeout=config.URL_FETCH_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()

            elements = data.get("content_elements") or []
            if not elements:
                print("  No content_elements; stopping.")
                break

            for el in elements:
                try:
                    if el.get("type") != "story":
                        continue

                    path = (((el.get("websites") or {}).get("uscannenberg") or {}).get("website_url"))
                    if not path:
                        continue

                    abs_url = urljoin("https://www.uscannenbergmedia.com", path)
                    if abs_url in processed_urls:
                        continue

                    title = ((el.get("headlines") or {}).get("basic")) or "Untitled"
                    snippet = ((el.get("description") or {}).get("basic")) or title

                    display_date = el.get("display_date")
                    url_date = None
                    article_date = None
                    if display_date:
                        try:
                            dt = datetime.fromisoformat(display_date.replace("Z", "+00:00"))
                            article_date = dt.date()
                            url_date = article_date.strftime("%Y-%m-%d")
                        except ValueError:
                            article_date = None
                            url_date = None

                    if article_date:
                        if article_date < start_date:
                            should_continue = False
                            continue
                        if article_date > end_date:
                            continue
                    elif config.NEWS_START_DATE:
                        continue

                    found_articles.append({
                        "title": title,
                        "url": abs_url,
                        "snippet": snippet,
                        "url_date": url_date,
                    })
                    processed_urls.add(abs_url)

                    if len(found_articles) >= config.MAX_SEARCH_RESULTS_TO_PROCESS:
                        break
                except Exception:
                    continue

            if len(found_articles) >= config.MAX_SEARCH_RESULTS_TO_PROCESS:
                break

            next_offset = data.get("next")
            if isinstance(next_offset, int) and next_offset > offset:
                offset = next_offset
            else:
                new_offset = offset + size
                if new_offset == offset:
                    print("  Next offset did not advance; stopping to avoid infinite loop.")
                    break
                offset = new_offset
            page_count += 1
        except requests.exceptions.RequestException as e:
            print(f"  Error accessing USC API at offset {offset}: {e}")
            break
        except ValueError as e:
            print(f"  Error parsing USC API response at offset {offset}: {e}")
            break
        except Exception as e:
            print(f"  Unexpected error at offset {offset}: {e}")
            break

    print(f"\nUSC API Summary:")
    print(f"  Total pages scanned: {page_count}")
    print(f"  Articles found in date range ({start_date} to {end_date}): {len(found_articles)}")
    return found_articles

    
def usc_scan_ubcnews_for_links() -> list[dict]:
    start_date, end_date = config.get_news_date_range()
    print(f"\n--- Scanning USC News for {start_date} to {end_date} ---")
    found_articles = []
    processed_urls = set()
    
    url = "https://www.cbsnews.com/tag/university-of-southern-california/"
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=config.URL_FETCH_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        side_bar_section = soup.find('section', id="component-list-latest-news")
        articles = side_bar_section.find_all('article')
        for article in articles:
            title = article.find('h4').text
            
            url = article.find('a')['href']
            # skip video articles
            if '/video/' in url:
                continue
            
            # Find article overview text
            overview = article.find('p', class_="item__dek").text
            
            dt_str = article.find('li', class_="item__date").text
            # Normalize article date and url_date (YYYY-MM-DD)
            if 'ago' in dt_str:
                article_date = end_date
                url_date = article_date.strftime('%Y-%m-%d')
            else:
                # e.g., "Apr 26, 2024" -> extract_ymd_from_text returns "YYYY-MM-DD" or None
                extracted = extract_ymd_from_text(dt_str)
                if not extracted:
                    continue
                url_date = extracted
                try:
                    article_date = datetime.strptime(extracted, "%Y-%m-%d").date()
                except ValueError:
                    continue
            # Compare using article_date (datetime.date) to avoid str vs date errors
            if article_date:
                if article_date < start_date:
                    continue
                if article_date > end_date:
                    continue
            
            found_articles.append({"title": title, "url": url, "snippet": title, "url_date": url_date})
            processed_urls.add(url)
    except Exception as e:
        print(f"Error fetching USC News page {url}: {e}")
        return []
    print(f"DEBUG: USC News found articles: {found_articles}")
    return found_articles


def usc_latimes_news_pages_for_links() -> list[dict]:
    start_date, end_date = config.get_news_date_range()
    print(f"\n--- Scanning USC LATimes News for {start_date} to {end_date} ---")
    found_articles = []
    processed_urls = set()
    
    url = "https://www.latimes.com/topic/education"
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=config.URL_FETCH_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        # New LATimes listing structure (per screenshot):
        # <div class="list-items">
        #   <div class="list-items-item" ...>
        #     <div class="promo-wrapper">
        #       <div class="promo-content">
        #         <div class="promo-title-container">...</div>
        #         <time class="promo-timestamp ..." datetime="2025-11-29T18:01:01.940Z"></time>
        #       </div>
        #     </div>
        #   </div>
        # </div>
        items = soup.select("div.list-items div.list-items-item")
        for li in items:
            a = (li.select_one("div.promo-content a[href]") or
                 li.select_one("a.tnt-asset-link[href]") or
                 li.select_one("a[href]"))
            if not a:
                continue

            title = (a.get_text(strip=True) or a.get("aria-label") or a.get("title") or "").strip()
            href = a['href']
            if not href:
                continue

            abs_url = urljoin(response.url, href)
            if abs_url in processed_urls:
                continue

            # Prefer a <time datetime="..."> if present; fallback to date extracted from URL
            url_date = None
            article_date = None
            t = li.select_one("div.promo-content time[datetime]")
            if t and t.has_attr("datetime"):
                try:
                    dt = datetime.fromisoformat(t["datetime"].replace("Z", "+00:00"))
                    article_date = dt.date()
                    url_date = article_date.strftime("%Y-%m-%d")
                except Exception:
                    article_date = None
                    url_date = None
            if article_date is None:
                url_date = extract_date_from_url(abs_url)
                if url_date:
                    try:
                        article_date = datetime.strptime(url_date, "%Y-%m-%d").date()
                    except ValueError:
                        article_date = None

            # Date window filtering only when we have a date
            if article_date:
                if article_date < start_date or article_date > end_date:
                    continue

            found_articles.append({
                "title": title or "Untitled",
                "url": abs_url,
                "snippet": title or "Untitled",
                "url_date": url_date
            })
            processed_urls.add(abs_url)   
            
    except Exception as e:
        print(f"Error fetching USC LATimes News page {url}: {e}")
        return []
    print(f"DEBUG: USC LATimes News found articles: {found_articles}")
    return found_articles
        
        
def usc_scan_uscnews_for_links() -> list[dict]:
    start_date, end_date = config.get_news_date_range()
    print(f"\n--- Scanning USC News for {start_date} to {end_date} ---")
    found_articles = []
    processed_urls = set()
    
    url = "https://today.usc.edu/category/university/"
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=config.URL_FETCH_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        articles = soup.find_all("article")
        for article in articles:
            title = article.find('h3').text.strip()
            url = article.find('a')['href']
            
            # e.g., Dec 5, 2025
            url_date_text = article.find('div', class_="f--field f--eyebrow date").find('span').text.strip()
            url_date = extract_ymd_from_text(url_date_text)
            if not url_date:
                continue
            # Convert to datetime.date for correct comparison
            try:
                article_date = datetime.strptime(url_date, "%Y-%m-%d").date()
            except ValueError:
                continue
            if article_date < start_date or article_date > end_date:
                continue
            
            if url in processed_urls:
                continue
            processed_urls.add(url)
            found_articles.append({"title": title, "url": url, "snippet": title, "url_date": url_date})
    except Exception as e:
        print(f"Error fetching USC News page {url}: {e}")
        return []
    print(f"DEBUG: USC News found articles: {found_articles}")
    return found_articles
            
            
def usc_scan_daily_trojan_for_links() -> list[dict]:
    start_date, end_date = config.get_news_date_range()
    print(f"\n--- Scanning Daily Trojan for {start_date} to {end_date} ---")
    found_articles = []
    processed_urls = set()

    url = "https://dailytrojan.com/category/news/"
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        response = requests.get(url, headers=headers, timeout=config.URL_FETCH_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        articles = soup.find_all("article")

        for article in articles:
            title_link = (
                article.select_one("h1 a[href]") or
                article.select_one("h2 a[href]") or
                article.select_one("h3 a[href]") or
                article.select_one("a[href]")
            )
            if not title_link:
                continue

            title = title_link.get_text(strip=True)
            href = title_link.get("href")
            if not title or not href:
                continue

            abs_url = urljoin(response.url, href)
            if abs_url in processed_urls:
                continue

            url_date = None
            article_date = None

            time_tag = article.select_one("time[datetime]")
            if time_tag and time_tag.get("datetime"):
                try:
                    dt = datetime.fromisoformat(time_tag["datetime"].replace("Z", "+00:00"))
                    article_date = dt.date()
                    url_date = article_date.strftime("%Y-%m-%d")
                except ValueError:
                    article_date = None
                    url_date = None

            if article_date is None:
                extracted = extract_ymd_from_text(article.get_text(" ", strip=True))
                if extracted:
                    url_date = extracted
                    try:
                        article_date = datetime.strptime(extracted, "%Y-%m-%d").date()
                    except ValueError:
                        article_date = None

            if article_date:
                if article_date < start_date or article_date > end_date:
                    continue
            elif config.NEWS_START_DATE:
                continue

            snippet_tag = article.select_one("p")
            snippet = snippet_tag.get_text(strip=True) if snippet_tag else title

            found_articles.append({
                "title": title,
                "url": abs_url,
                "snippet": snippet,
                "url_date": url_date,
            })
            processed_urls.add(abs_url)
    except Exception as e:
        print(f"Error fetching Daily Trojan page {url}: {e}")
        return []

    print(f"DEBUG: Daily Trojan found articles: {found_articles}")
    return found_articles


def usc_scan_archive_pages_for_date_range() -> list[dict]:
    annenberg_media_articles = usc_scan_annenberg_media_for_links()
    latimes_news_articles = usc_latimes_news_pages_for_links()
    usc_news_articles = usc_scan_uscnews_for_links()
    daily_trojan_articles = usc_scan_daily_trojan_for_links()
    return annenberg_media_articles + usc_news_articles + latimes_news_articles + daily_trojan_articles