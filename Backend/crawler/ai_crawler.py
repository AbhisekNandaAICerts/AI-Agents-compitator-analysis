#!/usr/bin/env python3
"""
AI-enabled link crawler — Playwright lifecycle fixed.

Usage:
  python ai_crawler_fixed_playwright.py --start-url https://www.netcomlearning.com/ --output netcom_links.json
"""

import argparse
import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from urllib.parse import urlparse, urljoin, urlunparse, parse_qsl

from dotenv import load_dotenv
load_dotenv()

import requests
from bs4 import BeautifulSoup
import urllib.robotparser as robotparser

# Playwright
try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
except Exception:
    async_playwright = None
    PlaywrightTimeoutError = Exception

# Optional OpenAI placeholder (not required)
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# Logging
logger = logging.getLogger('ai_crawler_fixed_playwright')
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# Constants
USER_AGENT = 'AICompanyLinkCrawler/1.0 (+https://example.com)'
HEADERS = {'User-Agent': USER_AGENT}
EXTENSION_SKIP = ('.jpg', '.jpeg', '.png', '.gif', '.svg', '.pdf', '.zip', '.rar', '.exe', '.tar', '.gz')
COMMON_MENU_SELECTORS = ['.menu', '.nav', '.dropdown', '[data-toggle]', '[aria-haspopup]', '.hamburger', '.menu-toggle']

# Optional OpenAI (disabled if not configured)
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
OPENAI_MODEL = os.environ.get('OPENAI_MODEL', 'gpt-4o-mini')
openai_client = None
if OpenAI is not None and OPENAI_API_KEY:
    try:
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
        logger.info('OpenAI client initialized (optional)')
    except Exception as e:
        logger.warning(f'Failed to initialize OpenAI: {e}')
        openai_client = None
else:
    logger.debug('OpenAI not configured or not installed; classifier disabled')

# --- helpers ------------------------------------------------------------------
def normalize_url(u, base=None):
    if not u:
        return None
    if base:
        u = urljoin(base, u)
    u = u.split('#', 1)[0]
    parsed = urlparse(u)
    scheme = (parsed.scheme or 'http').lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or '/'
    if path != '/' and path.endswith('/'):
        path = path.rstrip('/')
    qs_items = parse_qsl(parsed.query or '', keep_blank_values=True)
    qs_filtered = [(k, v) for (k, v) in qs_items if not (k.lower().startswith('utm_') or k.lower() in ('fbclid', 'gclid', 'icid'))]
    query = '&'.join(f'{k}={v}' for k, v in qs_filtered) if qs_filtered else ''
    normalized = urlunparse((scheme, netloc, path, '', query, ''))
    return normalized

def fetch_plain(url, timeout=15):
    logger.debug(f'fetch_plain start: {url}')
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        logger.debug(f'fetch_plain success: {url} (len={len(r.text)})')
        return r.text
    except Exception as e:
        logger.debug(f'fetch_plain failed {url}: {e}')
        return None

# Playwright fetch with interactions
async def fetch_with_playwright(context, url, wait_until='networkidle', timeout=40000):
    """
    Aggressive Playwright fetch:
     - goto(url)
     - scroll page to bottom (to trigger lazy loads)
     - hover / focus common menu selectors
     - safe click menu toggles
     - run JS collector for anchors, data-* attrs, onclick patterns
     - also extract URL-like strings and JSON URLs inside <script> tags
    Returns (html, list_of_raw_urls)
    """
    logger.debug(f'fetch_with_playwright start: {url}')
    page = await context.new_page()
    collected = set()
    try:
        await page.set_extra_http_headers({"User-Agent": USER_AGENT})
        await page.goto(url, wait_until=wait_until, timeout=timeout)
        await asyncio.sleep(0.4)

        # 1) Scroll slowly to bottom to trigger lazy-loading content
        try:
            viewport_h = await page.evaluate("() => window.innerHeight")
            total_h = await page.evaluate("() => document.body.scrollHeight")
            scroll_y = 0
            while scroll_y < total_h:
                scroll_y += int(viewport_h * 0.9)
                await page.evaluate(f'window.scrollTo(0, {scroll_y})')
                await asyncio.sleep(0.25)
                total_h = await page.evaluate("() => document.body.scrollHeight")
        except Exception:
            logger.debug('scrolling failed/partial (non-fatal)')

        # 2) Hover common menu selectors to reveal menus
        for sel in COMMON_MENU_SELECTORS:
            try:
                node = await page.query_selector(sel)
                if node:
                    try:
                        await node.hover()
                        await asyncio.sleep(0.12)
                    except Exception:
                        pass
            except Exception:
                pass

        # 3) Dispatch mouseenter/focus events to interactive elements
        try:
            await page.evaluate(
                """() => {
                    const els = Array.from(document.querySelectorAll('button, [role="button"], [data-toggle], [aria-haspopup], .menu, .nav'));
                    for (const el of els.slice(0,60)) {
                        try {
                            el.dispatchEvent(new Event('mouseenter', {bubbles:true}));
                            el.dispatchEvent(new Event('mouseover', {bubbles:true}));
                            el.focus && el.focus();
                        } catch(e){}
                    }
                    return true;
                }"""
            )
            await asyncio.sleep(0.15)
        except Exception:
            logger.debug('dispatch events failed (non-fatal)')

        # 4) Try clicking safe toggles (menu buttons) - best effort, avoid navigation
        try:
            toggles = await page.query_selector_all('button, [data-toggle], [aria-haspopup], .hamburger, .menu-toggle')
            for btn in toggles[:8]:
                try:
                    await btn.click(timeout=1200)
                    await asyncio.sleep(0.12)
                except Exception:
                    pass
        except Exception:
            pass

        # 5) JS collector for attributes/onclicks/links (same as before)
        js_collect_attrs = r"""
        () => {
          const urls = new Set();
          function add(u){ if(!u) return; urls.add(u); }
          // anchors
          for(const a of document.querySelectorAll('a[href]')) add(a.getAttribute('href'));
          // common data attributes
          const dataAttrs = ['data-href','data-url','data-link','data-target','data-path','data-route'];
          for(const attr of dataAttrs){
            for(const el of document.querySelectorAll('['+attr+']')){
              add(el.getAttribute(attr));
            }
          }
          // onclick patterns
          for(const el of document.querySelectorAll('[onclick]')){
            const s = el.getAttribute('onclick') || '';
            let m = s.match(/location\.href\s*=\s*['"]([^'"]+)['"]/);
            if(m) add(m[1]);
            m = s.match(/window\.location(?:\.href)?\s*=\s*['"]([^'"]+)['"]/);
            if(m) add(m[1]);
            m = s.match(/window\.open\(\s*['"]([^'"]+)['"]/);
            if(m) add(m[1]);
          }
          // link rels (canonical/prev/next)
          for(const link of document.querySelectorAll('link[href]')) {
            const rel = (link.getAttribute('rel') || '').toLowerCase();
            const href = link.getAttribute('href');
            if(rel && ['canonical','prev','next','alternate'].some(r=>rel.includes(r))) add(href);
          }
          // src/data attributes
          for(const el of document.querySelectorAll('[src],[data-href],[data-url]')){
            for(const k of ['src','data-href','data-url']) {
              const v = el.getAttribute(k); if(v) add(v);
            }
          }
          return Array.from(urls);
        }
        """
        try:
            collected_attrs = await page.evaluate(js_collect_attrs)
            for v in (collected_attrs or []):
                if v:
                    collected.add(v)
        except Exception:
            logger.debug('js attr collector failed (non-fatal)')

        # 6) Extract URL-like strings and JSON inside <script> tags (regex)
        try:
            scripts = await page.evaluate("""() => Array.from(document.querySelectorAll('script')).map(s=>s.textContent).filter(Boolean)""")
            # small regexes: http(s) URLs, and router-like paths (/courses/...), and quoted strings with slashes
            url_regex = re.compile(r'https?://[\\w\\-./?&=%#]+', re.I)
            path_regex = re.compile(r'(["\\\'])(\\/[-A-Za-z0-9_\\/:%?&=.,~+#-]+)\\1')  # quoted /path strings
            simple_path_regex = re.compile(r'\\/[-A-Za-z0-9_\\/:%?&=.,~+#-]+')  # /path...
            for s in (scripts or []):
                try:
                    # look for absolute urls
                    for m in url_regex.findall(s):
                        collected.add(m)
                    # look for quoted paths like "/courses/...'"
                    for m in path_regex.findall(s):
                        if len(m) > 1:
                            collected.add(m[1])
                    # also fallback any /path-looking substrings
                    for m in simple_path_regex.findall(s):
                        collected.add(m)
                except Exception:
                    continue
        except Exception:
            logger.debug('script content extraction failed (non-fatal)')

        # final page content
        content = await page.content()
        try:
            await page.close()
        except Exception:
            pass

        collected_list = list(collected)
        logger.info(f'fetch_with_playwright: {url} collected {len(collected_list)} raw candidates (sample {collected_list[:12]})')
        return content, collected_list

    except PlaywrightTimeoutError:
        logger.debug(f'PlaywrightTimeoutError for {url}')
        try:
            await page.close()
        except Exception:
            pass
        return None, []
    except Exception as e:
        logger.debug(f'Playwright fetch error {url}: {e}')
        try:
            await page.close()
        except Exception:
            pass
        return None, []

# HTML extraction
def extract_links_from_html(base_url, html):
    soup = BeautifulSoup(html, 'html.parser')
    found = set()
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if href:
            n = normalize_url(href, base=base_url)
            if n:
                found.add(n)
    for attr in ('data-href', 'data-url', 'data-link', 'data-target', 'data-path', 'data-route'):
        for el in soup.find_all(attrs={attr: True}):
            v = el.get(attr)
            if v:
                n = normalize_url(v, base=base_url)
                if n:
                    found.add(n)
    for el in soup.find_all(attrs={'onclick': True}):
        onclick = el.get('onclick') or ''
        m = re.search(r"""location\.href\s*=\s*['"]([^'"]+)['"]""", onclick)
        if not m:
            m = re.search(r"""window\.location(?:\.href)?\s*=\s*['"]([^'"]+)['"]""", onclick)
        if not m:
            m = re.search(r"""window\.open\(\s*['"]([^'"]+)['"]""", onclick)
        if m:
            n = normalize_url(m.group(1), base=base_url)
            if n:
                found.add(n)
    for tag in soup.find_all('link', href=True):
        rel = tag.get('rel') or []
        if isinstance(rel, list) and any(r in ('canonical', 'prev', 'next', 'alternate') for r in rel):
            n = normalize_url(tag['href'], base=base_url)
            if n:
                found.add(n)
    out = set()
    for u in found:
        if not u:
            continue
        lu = u.lower()
        if lu.startswith('mailto:') or lu.startswith('tel:'):
            continue
        if any(lu.endswith(ext) for ext in EXTENSION_SKIP):
            continue
        out.add(u)
    logger.info(f'Extracted {len(out)} normalized links from {base_url}')
    return out

def simple_classify(text):
    t = (text or '').lower()
    if 'course' in t or 'enroll' in t or 'training' in t:
        return 'course'
    if 'certif' in t or 'certificate' in t or 'exam' in t:
        return 'certification'
    if 'product' in t or 'buy' in t or 'price' in t:
        return 'product'
    if 'press' in t or 'news' in t or 'announcement' in t:
        return 'announcement'
    if 'blog' in t or 'case study' in t or 'case-study' in t:
        return 'blog'
    return 'other'

# Main crawl with correct async_playwright usage
async def crawl(start_url, output='netcom_links.json', max_pages=500, concurrency=3, delay=0.4, ignore_robots=False, no_playwright=False):
    logger.info(f'start crawl for {start_url}')
    parsed = urlparse(start_url)
    base_host = parsed.netloc
    base_root = f'{parsed.scheme}://{parsed.netloc}'

    rp = robotparser.RobotFileParser()
    try:
        rp.set_url(base_root + '/robots.txt')
        rp.read()
        logger.debug('robots.txt loaded')
    except Exception:
        rp = None
        logger.debug('robots.txt not loaded')

    start_norm = normalize_url(start_url)
    q = deque([start_norm])
    visited = set()
    discovered = set([start_norm])
    results = []

    use_playwright = (not no_playwright) and (async_playwright is not None)

    # If using Playwright, use async with pattern
    if use_playwright:
        logger.info('Playwright available; using browser rendering')
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=True)
                browser_context = await browser.new_context(user_agent=USER_AGENT)
                logger.info('Playwright browser/context started')
            except Exception as e:
                logger.warning(f'Playwright launch failed: {e}; falling back to requests-only')
                browser = None
                browser_context = None
                use_playwright = False

            async def worker(worker_id):
                nonlocal q, visited, discovered, results, browser_context, use_playwright
                logger.info(f'worker {worker_id} started')
                while q and len(visited) < max_pages:
                    try:
                        url = q.popleft()
                    except Exception:
                        break
                    if not url:
                        continue
                    if url in visited:
                        continue
                    if urlparse(url).netloc != base_host:
                        visited.add(url)
                        continue
                    if rp and not ignore_robots and not rp.can_fetch(USER_AGENT, url):
                        visited.add(url)
                        continue

                    logger.info(f'worker {worker_id} crawling: {url} (visited {len(visited)+1}/{max_pages})')
                    html = await asyncio.to_thread(fetch_plain, url)
                    extra_js_links = []
                    used_playwright = False
                    if (not html or len(html) < 500) and browser_context is not None:
                        logger.debug(f'worker {worker_id} using Playwright for {url}')
                        html, extra_js_links = await fetch_with_playwright(browser_context, url)
                        used_playwright = True

                    visited.add(url)
                    if not html:
                        logger.warning(f'worker {worker_id} failed to fetch {url}')
                        await asyncio.sleep(delay)
                        continue

                    links = extract_links_from_html(url, html)
                    for raw in (extra_js_links or []):
                        try:
                            n = normalize_url(raw, base=url)
                            if n:
                                links.add(n)
                        except Exception:
                            pass

                    logger.info(f'worker {worker_id} found {len(links)} links on {url}')

                    soup = BeautifulSoup(html, 'html.parser')
                    title = None
                    if soup.find('h1') and soup.find('h1').get_text(strip=True):
                        title = soup.find('h1').get_text(strip=True)
                    elif soup.title and soup.title.string:
                        title = soup.title.string.strip()
                    sample_text = (title or '') + ' ' + (soup.get_text(' ')[:800] or '')
                    classification = simple_classify(sample_text)

                    results.append({
                        'url': url,
                        'title': title,
                        'classification': classification,
                        'rendered_with_playwright': used_playwright,
                        'links_sample': sorted(list(links))[:40],
                    })
                    logger.info(f'worker {worker_id} saved {url} classified as {classification} (links_sample size {len(results[-1]["links_sample"])})')

                    enqueued = 0
                    for l in links:
                        if not l:
                            continue
                        if any(l.lower().endswith(ext) for ext in EXTENSION_SKIP):
                            continue
                        if urlparse(l).netloc == base_host and l not in discovered:
                            discovered.add(l)
                            q.append(l)
                            enqueued += 1
                    logger.debug(f'worker {worker_id} enqueued {enqueued} new links from {url}')

                    await asyncio.sleep(delay)

                logger.info(f'worker {worker_id} finished')

            tasks = [asyncio.create_task(worker(i)) for i in range(concurrency)]
            await asyncio.gather(*tasks)

            # close context and browser inside async_with (ensures clean shutdown)
            try:
                if browser_context is not None:
                    logger.info('closing Playwright context')
                    await browser_context.close()
                if browser is not None:
                    logger.info('closing Playwright browser')
                    await browser.close()
            except Exception as e:
                logger.warning(f'Error closing Playwright: {e}')

    else:
        logger.info('Playwright disabled or not available; using requests only')

        async def worker_no_pw(worker_id):
            nonlocal q, visited, discovered, results
            logger.info(f'worker {worker_id} started (no-playwright)')
            while q and len(visited) < max_pages:
                try:
                    url = q.popleft()
                except Exception:
                    break
                if not url:
                    continue
                if url in visited:
                    continue
                if urlparse(url).netloc != base_host:
                    visited.add(url)
                    continue
                if rp and not ignore_robots and not rp.can_fetch(USER_AGENT, url):
                    visited.add(url)
                    continue

                logger.info(f'worker {worker_id} crawling: {url} (visited {len(visited)+1}/{max_pages})')
                html = await asyncio.to_thread(fetch_plain, url)
                visited.add(url)
                if not html:
                    logger.warning(f'worker {worker_id} failed to fetch {url}')
                    await asyncio.sleep(delay)
                    continue

                links = extract_links_from_html(url, html)
                logger.info(f'worker {worker_id} found {len(links)} links on {url}')

                soup = BeautifulSoup(html, 'html.parser')
                title = None
                if soup.find('h1') and soup.find('h1').get_text(strip=True):
                    title = soup.find('h1').get_text(strip=True)
                elif soup.title and soup.title.string:
                    title = soup.title.string.strip()
                sample_text = (title or '') + ' ' + (soup.get_text(' ')[:800] or '')
                classification = simple_classify(sample_text)

                results.append({
                    'url': url,
                    'title': title,
                    'classification': classification,
                    'rendered_with_playwright': False,
                    'links_sample': sorted(list(links))[:40],
                })
                logger.info(f'worker {worker_id} saved {url} classified as {classification} (links_sample size {len(results[-1]["links_sample"])})')

                enqueued = 0
                for l in links:
                    if not l:
                        continue
                    if any(l.lower().endswith(ext) for ext in EXTENSION_SKIP):
                        continue
                    if urlparse(l).netloc == base_host and l not in discovered:
                        discovered.add(l)
                        q.append(l)
                        enqueued += 1
                logger.debug(f'worker {worker_id} enqueued {enqueued} new links from {url}')

                await asyncio.sleep(delay)

            logger.info(f'worker {worker_id} finished (no-playwright)')

        tasks = [asyncio.create_task(worker_no_pw(i)) for i in range(concurrency)]
        await asyncio.gather(*tasks)

    # write output
    out = {
        'start_url': start_url,
        'scraped_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'results': results,
        'visited_count': len(visited),
    }
    with open(output, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    logger.info(f'crawl complete. visited={len(visited)} saved={output}')

# CLI
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='AI-enabled link crawler (Playwright lifecycle fixed)')
    parser.add_argument('--start-url', required=True)
    parser.add_argument('--output', default='netcom_links.json')
    parser.add_argument('--max-pages', type=int, default=200)
    parser.add_argument('--concurrency', type=int, default=3)
    parser.add_argument('--delay', type=float, default=0.4)
    parser.add_argument('--ignore-robots', action='store_true')
    parser.add_argument('--no-playwright', action='store_true')
    args = parser.parse_args()

    try:
        asyncio.run(crawl(args.start_url, output=args.output, max_pages=args.max_pages, concurrency=args.concurrency, delay=args.delay, ignore_robots=args.ignore_robots, no_playwright=args.no_playwright))
    except KeyboardInterrupt:
        logger.info('Interrupted by user')
