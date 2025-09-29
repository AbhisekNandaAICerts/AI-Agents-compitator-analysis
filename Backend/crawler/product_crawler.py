"""
Sitemap-aware Playwright crawler for products, courses, and certifications
"""

import argparse
import asyncio
import json
import logging
import re
import time
from collections import deque
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
import urllib.robotparser as robotparser

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('sitemap_crawler')

USER_AGENT = 'ProductCourseCrawler/1.0 (+https://example.com)'
HEADERS = {'User-Agent': USER_AGENT}

PRICE_RE = re.compile(r"\b(?:\$|USD\s*)?\s?\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?\b")
COURSE_KEYWORDS = ['course', 'courses', '/course/', '/courses/', 'training', 'program', 'curriculum', 'skill', 'webinar', 'ebook']
PRODUCT_KEYWORDS = ['product', 'products', 'shop', 'store', 'item', 'sku']
CERTIFICATION_KEYWORDS = ['certification', 'certifications', '/certification/', '/certifications/']


def fetch_sitemap_urls(sitemap_url, timeout=15):
    try:
        r = requests.get(sitemap_url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        xml = r.text
    except Exception as e:
        logger.warning(f'Failed to fetch sitemap {sitemap_url}: {e}')
        return []

    try:
        root = ET.fromstring(xml)
    except Exception as e:
        logger.warning(f'Failed to parse sitemap XML from {sitemap_url}: {e}')
        return []

    urls = []
    tag_lower = root.tag.lower()
    if tag_lower.endswith('sitemapindex'):
        for sitemap in root.findall('.//{*}sitemap'):
            loc = sitemap.find('{*}loc')
            if loc is not None and loc.text:
                urls.extend(fetch_sitemap_urls(loc.text.strip()))
    else:
        for url_el in root.findall('.//{*}url'):
            loc = url_el.find('{*}loc')
            if loc is not None and loc.text:
                urls.append(loc.text.strip())
    if not urls:
        for loc in root.findall('.//{*}loc'):
            if loc.text:
                urls.append(loc.text.strip())
    return urls


class Robots:
    def __init__(self, base_url):
        parsed = urlparse(base_url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        self.rp = robotparser.RobotFileParser()
        try:
            self.rp.set_url(robots_url)
            self.rp.read()
        except Exception as e:
            logger.warning(f'Could not read robots.txt at {robots_url}: {e}')

    def can_fetch(self, url):
        try:
            return self.rp.can_fetch(USER_AGENT, url)
        except Exception:
            return True


def find_jsonld(soup):
    found = []
    for script in soup.find_all('script', type='application/ld+json'):
        text = script.string
        if not text:
            continue
        try:
            data = json.loads(text)
        except Exception:
            try:
                data = json.loads(re.sub(r"\s+", ' ', text))
            except Exception:
                continue
        if isinstance(data, list):
            found.extend(data)
        else:
            found.append(data)
    return found


def is_item_by_jsonld(jsonlds):
    for item in jsonlds:
        if not isinstance(item, dict):
            continue
        itype = item.get('@type') or item.get('type')
        if not itype:
            if 'graph' in item and isinstance(item.get('graph'), list):
                for g in item.get('graph', []):
                    t = g.get('@type') or g.get('type')
                    if t and ('Product' in t or 'Course' in t or 'Certification' in t):
                        return True
            continue
        if isinstance(itype, list):
            for t in itype:
                if 'Product' in t or 'Course' in t or 'Certification' in t:
                    return True
        else:
            if 'Product' in itype or 'Course' in itype or 'Certification' in itype:
                return True
    return False


def heuristics_is_item(url, soup, html_text):
    url_lower = (url or '').lower()
    jsonlds = find_jsonld(soup)
    if is_item_by_jsonld(jsonlds):
        return True
    tokens = COURSE_KEYWORDS + PRODUCT_KEYWORDS + CERTIFICATION_KEYWORDS
    if any(tok in url_lower for tok in tokens):
        return True
    page_text = (html_text or '').lower()
    if PRICE_RE.search(page_text) and any(k in page_text for k in tokens):
        return True
    triggers = ['add to cart', 'add to basket', 'buy now', 'purchase', 'enroll', 'register', 'book now']
    if any(t in page_text for t in triggers):
        return True
    return False


def extract_item_data(url, soup, html_text):
    item = {'url': url}
    h1 = soup.find('h1')
    if h1 and h1.get_text(strip=True):
        item['title'] = h1.get_text(strip=True)
    else:
        og = soup.find('meta', property='og:title') or soup.find('meta', attrs={'name': 'title'})
        if og and og.get('content'):
            item['title'] = og['content'].strip()
        elif soup.title and soup.title.string:
            item['title'] = soup.title.string.strip()

    desc = soup.find('meta', property='og:description') or soup.find('meta', attrs={'name': 'description'})
    if desc and desc.get('content'):
        item['description'] = desc['content'].strip()
    else:
        for p in soup.find_all('p'):
            t = p.get_text(strip=True)
            if t and len(t) > 60:
                item['description'] = t
                break

    images = set()
    og_img = soup.find('meta', property='og:image')
    if og_img and og_img.get('content'):
        images.add(urljoin(url, og_img['content'].strip()))
    for img in soup.find_all('img'):
        src = img.get('src') or img.get('data-src') or img.get('data-original')
        if src:
            images.add(urljoin(url, src))
    if images:
        item['images'] = list(images)

    jsonlds = find_jsonld(soup)
    if jsonlds:
        item['jsonld'] = jsonlds
        for j in jsonlds:
            if isinstance(j, dict) and 'offers' in j:
                offers = j.get('offers')
                if isinstance(offers, dict):
                    price = offers.get('price')
                    currency = offers.get('priceCurrency')
                    if price:
                        item['price'] = str(price)
                    if currency:
                        item['currency'] = currency
                        break

    if 'price' not in item:
        prices = PRICE_RE.findall(html_text or '')
        if prices:
            item['price'] = prices[0]

    m = re.search(r"(\d+\s*(?:hour|hours|day|days|week|weeks|month|months))", html_text or '', re.I)
    if m:
        item['duration'] = m.group(1)

    for lvl in ['beginner', 'intermediate', 'advanced']:
        if lvl in (html_text or '').lower():
            item['level'] = lvl
            break

    for attr in ['sku', 'data-sku', 'product-id', 'data-product-id', 'productid']:
        el = soup.find(attrs={attr: True})
        if el:
            item['sku'] = el.get(attr)
            break

    return item


async def fetch_with_playwright(context, url, wait_until='networkidle', timeout=30000):
    page = await context.new_page()
    try:
        await page.set_extra_http_headers({"User-Agent": USER_AGENT})
        await page.goto(url, wait_until=wait_until, timeout=timeout)
        await asyncio.sleep(0.2)
        content = await page.content()
        await page.close()
        return content
    except PlaywrightTimeoutError:
        logger.debug(f'Playwright timeout for {url}')
        try:
            await page.close()
        except Exception:
            pass
        return None
    except Exception as e:
        logger.debug(f'Playwright failed for {url}: {e}')
        try:
            await page.close()
        except Exception:
            pass
        return None


def fetch_plain(url, timeout=15):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logger.debug(f'Plain fetch failed for {url}: {e}')
        return None


async def crawl_from_sitemaps(sitemaps, output='products.json', max_pages=1000, concurrency=4, delay=0.5, ignore_robots=False):
    logger.info('Fetching sitemaps...')
    all_urls = []
    for s in sitemaps:
        urls = await asyncio.to_thread(fetch_sitemap_urls, s)
        logger.info(f'Found {len(urls)} URLs in {s}')
        all_urls.extend(urls)

    seen_urls = set()
    urls = []
    for u in all_urls:
        if u not in seen_urls:
            seen_urls.add(u)
            urls.append(u)

    logger.info(f'Total unique URLs from sitemaps: {len(urls)}')
    if not urls:
        logger.warning('No URLs found in provided sitemaps. Exiting.')
        return

    base_host = urlparse(sitemaps[0]).netloc
    robots = Robots(sitemaps[0]) if not ignore_robots else None

    q = deque([u for u in urls if urlparse(u).netloc == base_host])
    visited = set()
    items = []
    discovered = set(seen_urls)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT)

        async def worker(worker_id):
            nonlocal q, visited, items, discovered
            while True:
                try:
                    url = q.popleft()
                except IndexError:
                    break
                if len(visited) >= max_pages:
                    break
                if url in visited:
                    continue
                if urlparse(url).netloc != base_host:
                    visited.add(url)
                    continue
                if robots and not robots.can_fetch(url):
                    logger.debug(f'Blocked by robots: {url}')
                    visited.add(url)
                    continue

                html = await asyncio.to_thread(fetch_plain, url)
                used_playwright = False
                if not html or len(html) < 200:
                    html = await fetch_with_playwright(context, url)
                    used_playwright = True

                visited.add(url)
                if not html:
                    await asyncio.sleep(delay)
                    continue

                soup = BeautifulSoup(html, 'html.parser')
                html_text = soup.get_text(' ')

                if heuristics_is_item(url, soup, html_text):
                    data = extract_item_data(url, soup, html_text)
                    data['_rendered_with_playwright'] = used_playwright
                    items.append(data)
                    logger.info(f'Worker {worker_id}: Found item -> {data.get("title") or url}')

                for a in soup.find_all('a', href=True):
                    href = a['href'].strip()
                    if href.lower().startswith('mailto:') or href.lower().startswith('tel:'):
                        continue
                    joined = urljoin(url, href.split('#')[0])
                    if urlparse(joined).netloc == base_host and joined not in discovered:
                        discovered.add(joined)
                        q.append(joined)

                await asyncio.sleep(delay)

        tasks = [asyncio.create_task(worker(i)) for i in range(concurrency)]
        await asyncio.gather(*tasks)

        await browser.close()

    out = {
        'start_sitemaps': sitemaps,
        'scraped_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'items_found': items,
        'visited_count': len(visited),
    }
    with open(output, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    logger.info(f'Crawl complete. Visited {len(visited)} pages. Found {len(items)} items. Saved to {output}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--sitemaps', nargs='+', required=True)
    parser.add_argument('--output', default='products.json')
    parser.add_argument('--max-pages', type=int, default=1000)
    parser.add_argument('--concurrency', type=int, default=4)
    parser.add_argument('--delay', type=float, default=0.5)
    parser.add_argument('--ignore-robots', action='store_true')

    args = parser.parse_args()

    sitemaps = []
    for s in args.sitemaps:
        if s.startswith('http'):
            sitemaps.append(s)
        else:
            try:
                with open(s, 'r', encoding='utf-8') as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            sitemaps.append(line)
            except Exception:
                sitemaps.append(s)

    asyncio.run(crawl_from_sitemaps(sitemaps, output=args.output, max_pages=args.max_pages, concurrency=args.concurrency, delay=args.delay, ignore_robots=args.ignore_robots))
