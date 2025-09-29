#!/usr/bin/env python3
"""
Robust website crawler that finds relevant links (courses, products, announcements, certifications, blogs, careers, investors, etc).
Features:
 - Seeds crawl from start URL and (optionally) sitemaps if found or requested.
 - Uses Playwright (when available) to render JS, hover/click menus, scroll, and evaluate page JS to collect links.
 - Scans inline <script> JSON/text for URL-like strings and router paths.
 - Normalizes and deduplicates URLs, removes tracking params, handles fragments, trailing slash differences.
 - Respects robots.txt by default (optionally ignored).
 - Detailed logging of progress and everything discovered.
Usage example:
  pip install playwright requests beautifulsoup4
  playwright install
  python robust_crawler.py --start-url https://www.netcomlearning.com/ --output netcom_all_links.json --max-pages 100 --concurrency 4 --use-sitemaps
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
import xml.etree.ElementTree as ET
import urllib.robotparser as robotparser

try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
except Exception:
    async_playwright = None
    PlaywrightTimeoutError = Exception

# Logging
logger = logging.getLogger('robust_crawler')
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)

USER_AGENT = 'RobustSiteCrawler/1.0 (+https://example.com)'
HEADERS = {'User-Agent': USER_AGENT}
EXTENSION_SKIP = ('.jpg', '.jpeg', '.png', '.gif', '.svg', '.pdf', '.zip', '.rar', '.exe', '.tar', '.gz', '.woff', '.woff2')
COMMON_MENU_SELECTORS = ['.menu', '.nav', '.dropdown', '[data-toggle]', '[aria-haspopup]', '.hamburger', '.menu-toggle']

# regexes for script scanning
ABS_URL_RE = re.compile(r'https?://[^\s"\'<>]+', re.I)
QUOTED_PATH_RE = re.compile(r'["\'](\/[A-Za-z0-9_\-\/.%?&=+#~]+)["\']')
SIMPLE_PATH_RE = re.compile(r'\/[A-Za-z0-9_\-\/.%?&=+#~]+')

def normalize_url(u, base=None):
    if not u:
        return None
    try:
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
        if netloc == '':
            return None
        return normalized
    except Exception:
        return None

def fetch_plain(url, timeout=15):
    logger.debug(f'HTTP fetch: {url}')
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logger.debug(f'HTTP fetch failed {url}: {e}')
        return None

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

async def fetch_with_playwright(context, url, wait_until='networkidle', timeout=35000):
    page = await context.new_page()
    try:
        await page.set_extra_http_headers({"User-Agent": USER_AGENT})
        await page.goto(url, wait_until=wait_until, timeout=timeout)
        await asyncio.sleep(0.35)

        # scroll slowly to trigger lazy-load content
        try:
            viewport_h = await page.evaluate("() => window.innerHeight")
            total_h = await page.evaluate("() => document.body.scrollHeight")
            scrolled = 0
            while scrolled < total_h:
                scrolled += int(viewport_h * 0.9)
                await page.evaluate(f'window.scrollTo(0, {scrolled})')
                await asyncio.sleep(0.20)
                total_h = await page.evaluate("() => document.body.scrollHeight")
        except Exception:
            pass

        # hover common selectors to reveal navs
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

        # dispatch mouseenter/focus to interactive elements
        try:
            await page.evaluate("""() => {
                const els = Array.from(document.querySelectorAll('button, [role="button"], .menu, .nav, [data-toggle], [aria-haspopup]'));
                for (const el of els.slice(0, 60)) {
                    try {
                        el.dispatchEvent(new Event('mouseenter', {bubbles:true}));
                        el.dispatchEvent(new Event('mouseover', {bubbles:true}));
                        el.focus && el.focus();
                    } catch(e){}
                }
                return true;
            }""")
            await asyncio.sleep(0.18)
        except Exception:
            pass

        # try clicking safe toggles (menu openers)
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

        # collect anchors and data attrs/onclicks via evaluation
        js_collect = r"""
        () => {
          const urls = new Set();
          function add(u){ if(!u) return; urls.add(u); }
          for(const a of document.querySelectorAll('a[href]')) add(a.getAttribute('href'));
          const dataAttrs = ['data-href','data-url','data-link','data-target','data-path','data-route'];
          for(const attr of dataAttrs){
            for(const el of document.querySelectorAll('['+attr+']')){
              add(el.getAttribute(attr));
            }
          }
          for(const el of document.querySelectorAll('[onclick]')){
            const s = el.getAttribute('onclick') || '';
            let m = s.match(/location\.href\s*=\s*['"]([^'"]+)['"]/);
            if(m) add(m[1]);
            m = s.match(/window\.location(?:\.href)?\s*=\s*['"]([^'"]+)['"]/);
            if(m) add(m[1]);
            m = s.match(/window\.open\(\s*['"]([^'"]+)['"]/);
            if(m) add(m[1]);
          }
          for(const link of document.querySelectorAll('link[href]')) {
            const rel = (link.getAttribute('rel') || '').toLowerCase();
            const href = link.getAttribute('href');
            if(rel && ['canonical','prev','next','alternate'].some(r=>rel.includes(r))) add(href);
          }
          for(const el of document.querySelectorAll('[src],[data-href],[data-url]')) {
            for(const k of ['src','data-href','data-url']) {
              const v = el.getAttribute(k); if(v) add(v);
            }
          }
          return Array.from(urls);
        }
        """
        collected_attrs = []
        try:
            collected_attrs = await page.evaluate(js_collect)
        except Exception:
            collected_attrs = []

        # also extract script text for URLs and router-like paths
        script_texts = []
        try:
            script_texts = await page.evaluate("() => Array.from(document.querySelectorAll('script')).map(s=>s.textContent).filter(Boolean)")
        except Exception:
            script_texts = []

        content = await page.content()
        try:
            await page.close()
        except Exception:
            pass

        # gather raw candidates
        candidates = set()
        for v in (collected_attrs or []):
            if v:
                candidates.add(v)
        for s in (script_texts or []):
            for m in ABS_URL_RE.findall(s):
                candidates.add(m)
            for q in QUOTED_PATH_RE.findall(s):
                if isinstance(q, tuple):
                    # QUOTED_PATH_RE captures groups, pick group that looks like path
                    for g in q:
                        if g and g.startswith('/'):
                            candidates.add(g)
                else:
                    if q and q.startswith('/'):
                        candidates.add(q)
            for p in SIMPLE_PATH_RE.findall(s):
                if p and p.startswith('/'):
                    candidates.add(p)
        raw_list = list(candidates)
        logger.debug(f'Playwright collected {len(raw_list)} raw candidates on {url}')
        return content, raw_list

    except PlaywrightTimeoutError:
        try:
            await page.close()
        except Exception:
            pass
        return None, []
    except Exception as e:
        try:
            await page.close()
        except Exception:
            pass
        logger.debug(f'Playwright fetch error {url}: {e}')
        return None, []

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
    logger.debug(f'HTML extractor found {len(out)} links on {base_url}')
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
    if 'career' in t or 'job' in t or 'join us' in t:
        return 'careers'
    return 'other'

async def robust_crawl(start_url,
                       output='out_links.json',
                       max_pages=1000,
                       concurrency=4,
                       delay=0.35,
                       ignore_robots=False,
                       use_sitemaps=False,
                       no_playwright=False,
                       menu_selectors=None):
    logger.info(f'Starting crawl: {start_url}')
    parsed = urlparse(start_url)
    base_host = parsed.netloc
    base_root = f'{parsed.scheme}://{parsed.netloc}'

    rp = robotparser.RobotFileParser()
    try:
        rp.set_url(base_root + '/robots.txt')
        rp.read()
        logger.debug('Loaded robots.txt')
    except Exception:
        rp = None
        logger.debug('No robots.txt or failed to load')

    initial_urls = set()
    initial_urls.add(normalize_url(start_url))

    if use_sitemaps:
        # try to fetch common sitemap locations and parse
        sitemap_candidates = [f'{base_root}/sitemap.xml', f'{base_root}/sitemap_index.xml', f'{base_root}/sitemap-index.xml', f'{base_root}/sitemap.xml.gz']
        found_sitemaps = set()
        for s in sitemap_candidates:
            try:
                r = requests.get(s, headers=HEADERS, timeout=8)
                if r.status_code == 200 and '<urlset' in r.text.lower():
                    found_sitemaps.add(s)
            except Exception:
                pass
        # try common sitemap index at root
        if not found_sitemaps:
            # crawl start page for sitemap links
            html = fetch_plain(start_url)
            if html:
                soup = BeautifulSoup(html, 'html.parser')
                for a in soup.find_all('a', href=True):
                    href = a['href']
                    if href and 'sitemap' in href:
                        full = normalize_url(href, base=start_url)
                        if full:
                            found_sitemaps.add(full)
        for s in found_sitemaps:
            try:
                urls = fetch_sitemap_urls(s)
                for u in urls:
                    nu = normalize_url(u)
                    if nu:
                        initial_urls.add(nu)
                logger.info(f'Loaded {len(urls)} urls from sitemap {s}')
            except Exception:
                pass

    # general crawling queue seeded with initial_urls
    q = deque(sorted(initial_urls))
    discovered = set(initial_urls)
    visited = set()
    results = []

    use_pw = (not no_playwright) and (async_playwright is not None)
    if menu_selectors:
        COMMON_MENU_SELECTORS.extend(menu_selectors)

    sem = asyncio.Semaphore(concurrency)

    async def worker(worker_id):
        nonlocal q, discovered, visited, results, use_pw
        logger.info(f'Worker {worker_id} started')
        # create a local Playwright browser context if using Playwright (create new browser per worker would be heavy)
        browser_context = None
        if use_pw:
            # We'll use a shared browser context from outer control in main; to keep lifecycle predictable we will reuse a single context.
            pass
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
                logger.debug(f'Blocked by robots for {url}')
                visited.add(url)
                continue

            logger.info(f'Worker {worker_id} crawling: {url} ({len(visited)+1}/{max_pages})')
            html = await asyncio.to_thread(fetch_plain, url)
            raw_js_candidates = []
            used_playwright = False
            # If page seems minimal or we want to render, use playwright
            if (not html or len(html) < 600) and use_pw:
                # Playwright usage handled centrally by main loop to avoid multiple contexts; we will request rendering via shared queue
                # For simplicity we will not initialize browser here — main will provide rendered pages via a shared async function (see below).
                pass

            # We'll attempt to render using a helper if Playwright is enabled (main supplies 'render_page' coroutine)
            if (not html or len(html) < 600) and use_pw:
                try:
                    content, raw_js_candidates = await render_page(url)
                    html = content or html
                    used_playwright = True
                except Exception as e:
                    logger.debug(f'Playwright render failed for {url}: {e}')

            visited.add(url)
            if not html:
                logger.warning(f'No HTML for {url}')
                await asyncio.sleep(delay)
                continue

            links = extract_links_from_html(url, html)
            # merge raw_js_candidates
            for raw in (raw_js_candidates or []):
                try:
                    n = normalize_url(raw, base=url)
                    if n:
                        links.add(n)
                except Exception:
                    pass

            logger.info(f'Worker {worker_id} found {len(links)} links on {url}')

            # classification sample
            soup = BeautifulSoup(html, 'html.parser')
            title = None
            if soup.find('h1') and soup.find('h1').get_text(strip=True):
                title = soup.find('h1').get_text(strip=True)
            elif soup.title and soup.title.string:
                title = soup.title.string.strip()
            sample_text = (title or '') + ' ' + (soup.get_text(' ')[:1200] or '')
            classification = simple_classify(sample_text)

            results.append({
                'url': url,
                'title': title,
                'classification': classification,
                'rendered_with_playwright': used_playwright,
                'links_sample': sorted(list(links))[:60],
            })

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
            logger.debug(f'Worker {worker_id} enqueued {enqueued} new links from {url}')

            await asyncio.sleep(delay)
        logger.info(f'Worker {worker_id} finished')

    render_lock = asyncio.Lock()
    shared_browser = {'browser': None, 'context': None, 'pw': None}

    async def start_playwright():
        if not use_pw:
            return
        try:
            pw = await async_playwright().__aenter__()
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=USER_AGENT)
            shared_browser['pw'] = pw
            shared_browser['browser'] = browser
            shared_browser['context'] = context
            logger.info('Playwright started (shared context)')
        except Exception as e:
            logger.warning(f'Failed to start Playwright: {e}')
            shared_browser['pw'] = shared_browser['browser'] = shared_browser['context'] = None

    async def stop_playwright():
        try:
            ctx = shared_browser.get('context')
            br = shared_browser.get('browser')
            pw = shared_browser.get('pw')
            if ctx is not None:
                logger.info('Closing Playwright context')
                await ctx.close()
            if br is not None:
                logger.info('Closing Playwright browser')
                await br.close()
            if pw is not None:
                try:
                    await pw.__aexit__(None, None, None)
                    logger.info('Playwright stopped')
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f'Error during Playwright stop: {e}')

    async def render_page(url):
        # single shared renderer that uses the shared browser/context
        if shared_browser.get('context') is None:
            raise RuntimeError('Playwright not available')
        async with render_lock:
            try:
                ctx = shared_browser['context']
                content, candidates = await fetch_with_playwright(ctx, url)
                return content, candidates
            except Exception as e:
                logger.debug(f'render_page error: {e}')
                return None, []

    # start playwright if requested
    if use_pw:
        await start_playwright()
        if shared_browser.get('context') is None:
            logger.info('Playwright unavailable; falling back to requests-only')
            use_pw = False

    # if no-playwright, ensure render_page raises
    if not use_pw:
        async def render_page(_):
            return None, []

    # concurrency: spawn workers
    tasks = []
    for i in range(concurrency):
        tasks.append(asyncio.create_task(worker(i)))
    await asyncio.gather(*tasks)

    # stop playwright cleanly
    if use_pw:
        await stop_playwright()

    # write results
    out = {
        'start_url': start_url,
        'scraped_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'results': results,
        'visited_count': len(visited),
        'discovered_count': len(discovered),
    }
    with open(output, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    logger.info(f'Crawl finished. Visited {len(visited)} pages. Discovered {len(discovered)} total links. Saved to {output}')

def parse_cli():
    p = argparse.ArgumentParser(description='Robust website crawler')
    p.add_argument('--start-url', required=True)
    p.add_argument('--output', default='out_links.json')
    p.add_argument('--max-pages', type=int, default=1000)
    p.add_argument('--concurrency', type=int, default=4)
    p.add_argument('--delay', type=float, default=0.35)
    p.add_argument('--ignore-robots', action='store_true')
    p.add_argument('--use-sitemaps', action='store_true')
    p.add_argument('--no-playwright', action='store_true')
    p.add_argument('--menu-selectors', nargs='*', help='Extra CSS selectors (space separated) to hover/click to reveal navs', default=[])
    return p.parse_args()

if __name__ == '__main__':
    args = parse_cli()
    if args.menu_selectors:
        COMMON_MENU_SELECTORS.extend(args.menu_selectors)
    try:
        asyncio.run(robust_crawl(args.start_url,
                                output=args.output,
                                max_pages=args.max_pages,
                                concurrency=args.concurrency,
                                delay=args.delay,
                                ignore_robots=args.ignore_robots,
                                use_sitemaps=args.use_sitemaps,
                                no_playwright=args.no_playwright,
                                menu_selectors=args.menu_selectors))
    except KeyboardInterrupt:
        logger.info('Interrupted by user')
