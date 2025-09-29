#!/usr/bin/env python3
"""
keyword_crawler.py

Crawl a website while respecting robots.txt and collect links related to:
 - products
 - courses
 - certifications

Usage:
    python keyword_crawler.py https://example.com --max-pages 500 --delay 1.0

Outputs a JSON file with found links (default: found_links.json)
"""

import argparse
import json
import re
import sys
import time
import threading
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup
from urllib import robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque

# ---------------------------
# Helper utilities
# ---------------------------

KEYWORD_PATTERNS = [
    re.compile(r"\bproduct(s)?\b", re.IGNORECASE),
    re.compile(r"\bcourse(s)?\b", re.IGNORECASE),
    re.compile(r"certif(y|icate|ication|ications)\b", re.IGNORECASE),
]

DEFAULT_USER_AGENT = "KeywordCrawler/1.0 (+https://example.com/bot)"

def looks_relevant(url: str, anchor_text: str = "") -> bool:
    """
    Decide whether a link is related to products, courses, or certifications
    by checking the path and the anchor text (if any).
    """
    combined = (url + " " + (anchor_text or "")).lower()
    for p in KEYWORD_PATTERNS:
        if p.search(combined):
            return True
    return False

def normalize_url(base: str, link: str) -> str | None:
    """Resolve relative URLs, remove fragments, and normalize."""
    if not link:
        return None
    try:
        joined = urljoin(base, link)
    except Exception:
        return None
    # remove fragments like #section
    cleaned, _ = urldefrag(joined)
    return cleaned

# ---------------------------
# Robots parser wrapper
# ---------------------------

class Robots:
    def __init__(self, site_url: str, user_agent: str):
        parsed = urlparse(site_url)
        scheme = parsed.scheme or "https"
        netloc = parsed.netloc or parsed.path  # fallback
        robots_url = f"{scheme}://{netloc}/robots.txt"
        self.rp = robotparser.RobotFileParser()
        self.rp.set_url(robots_url)
        try:
            self.rp.read()
        except Exception:
            # If robots can't be read, we'll default to permissive
            pass
        self.user_agent = user_agent
        # Try to parse crawl-delay from robots.txt manually (best-effort)
        self.crawl_delay = None
        try:
            txt = requests.get(robots_url, timeout=6, headers={"User-Agent": user_agent}).text
            # search for Crawl-delay under our UA or under wildcard (*)
            # simple heuristic
            ua_blocks = re.split(r"\n(?=User-agent:)", txt, flags=re.IGNORECASE)
            for block in ua_blocks:
                header = re.search(r"User-agent:\s*(.*)", block, flags=re.IGNORECASE)
                if not header:
                    continue
                ua = header.group(1).strip()
                if ua == "*" or ua.lower() in user_agent.lower():
                    m = re.search(r"Crawl-delay:\s*([0-9]+(?:\.[0-9]+)?)", block, flags=re.IGNORECASE)
                    if m:
                        self.crawl_delay = float(m.group(1))
                        break
        except Exception:
            self.crawl_delay = None

    def can_fetch(self, url: str) -> bool:
        try:
            return self.rp.can_fetch(self.user_agent, url)
        except Exception:
            return True

# ---------------------------
# Crawler
# ---------------------------

class KeywordCrawler:
    def __init__(
        self,
        start_url: str,
        user_agent: str = DEFAULT_USER_AGENT,
        max_pages: int = 1000,
        delay: float = 1.0,
        concurrency: int = 5,
        output: str = "found_links.json",
    ):
        self.start_url = start_url.rstrip("/")
        self.base_domain = urlparse(self.start_url).netloc
        self.scheme = urlparse(self.start_url).scheme or "https"
        self.user_agent = user_agent
        self.max_pages = max_pages
        self.delay = delay
        self.concurrency = max(1, concurrency)
        self.output = output

        self.robots = Robots(self.start_url, self.user_agent)
        # If robots.txt specifies crawl-delay, prefer it
        if self.robots.crawl_delay is not None:
            self.delay = max(self.delay, float(self.robots.crawl_delay))

        self.to_visit = deque([self.start_url])
        self.visited = set()
        self.found_links = {}  # url -> {"anchor": ..., "source": ...}
        self.lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.user_agent})

    def same_domain(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.netloc == self.base_domain or parsed.netloc == ""

    def fetch(self, url: str) -> tuple[int, str] | None:
        try:
            resp = self.session.get(url, timeout=10)
            return resp.status_code, resp.text
        except Exception as e:
            return None

    def extract_links(self, html: str, base_url: str):
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a.get("href")
            anchor = (a.get_text() or "").strip()
            normalized = normalize_url(base_url, href)
            if not normalized:
                continue
            yield normalized, anchor

    def process_page(self, url: str):
        # Respect robots.txt
        if not self.robots.can_fetch(url):
            return

        # fetch
        result = self.fetch(url)
        if not result:
            return
        status, html = result
        if status >= 400 or html is None:
            return

        # parse links
        for link, anchor in self.extract_links(html, url):
            # ignore mailto:, tel:, javascript:
            if link.startswith(("mailto:", "tel:", "javascript:")):
                continue
            # keep to same domain only
            if not self.same_domain(link):
                continue
            with self.lock:
                if link not in self.visited and link not in self.to_visit:
                    # add to queue for crawling later (BFS)
                    if len(self.visited) + len(self.to_visit) < self.max_pages:
                        self.to_visit.append(link)
            # if relevant, record
            if looks_relevant(link, anchor):
                with self.lock:
                    if link not in self.found_links:
                        self.found_links[link] = {"anchor": anchor, "found_on": url}

    def run(self):
        """
        Main loop. Uses a ThreadPoolExecutor to fetch/process pages concurrently,
        but still respects a global politeness delay between starting requests.
        """
        with ThreadPoolExecutor(max_workers=self.concurrency) as exe:
            futures = {}
            while self.to_visit and len(self.visited) < self.max_pages:
                url = self.to_visit.popleft()
                # Normalize and ensure same domain
                parsed = urlparse(url)
                if not parsed.scheme:
                    # relative path
                    url = self.scheme + "://" + self.base_domain + url
                url = url.rstrip("/")

                # robots check early
                if not self.robots.can_fetch(url):
                    continue

                with self.lock:
                    if url in self.visited:
                        continue
                    self.visited.add(url)

                # submit task
                fut = exe.submit(self.process_page, url)
                futures[fut] = url

                # Politeness: do not start bursty requests. Sleep small time between submissions.
                time.sleep(self.delay / max(1, self.concurrency))

                # Clean up completed futures to free memory and raise exceptions if any
                done = [f for f in futures if f.done()]
                for f in done:
                    try:
                        _ = f.result()
                    except Exception as e:
                        # we do not crash the whole crawler on single-page failure
                        pass
                    del futures[f]

            # wait for remaining tasks to complete
            for fut in as_completed(list(futures.keys())):
                try:
                    fut.result()
                except Exception:
                    pass

        # write results
        self.save_results()

    def save_results(self):
        # Prepare output dictionary sorted for readability
        out = []
        for url, meta in sorted(self.found_links.items()):
            out.append({"url": url, "anchor": meta.get("anchor", ""), "found_on": meta.get("found_on", "")})
        with open(self.output, "w", encoding="utf-8") as f:
            json.dump({"start_url": self.start_url, "found_count": len(out), "links": out}, f, indent=2, ensure_ascii=False)
        print(f"Saved {len(out)} relevant links to {self.output}")

# ---------------------------
# CLI
# ---------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Crawl a site and collect product/course/certification links (obeys robots.txt)")
    p.add_argument("start_url", help="Starting URL to crawl (e.g. https://example.com)")
    p.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="User-Agent header")
    p.add_argument("--max-pages", type=int, default=1000, help="Maximum number of pages to visit")
    p.add_argument("--delay", type=float, default=1.0, help="Politeness delay (seconds) between requests")
    p.add_argument("--concurrency", type=int, default=5, help="Number of worker threads")
    p.add_argument("--output", default="found_links.json", help="Output JSON file for found links")
    p.add_argument("--include-patterns", nargs="*", help="Additional regex patterns to treat as 'relevant'")
    return p.parse_args()

def main():
    args = parse_args()
    # if user supplied extra patterns, add them
    global KEYWORD_PATTERNS
    if args.include_patterns:
        for pat in args.include_patterns:
            try:
                KEYWORD_PATTERNS.append(re.compile(pat, re.IGNORECASE))
            except re.error:
                print(f"Invalid regex pattern skipped: {pat}", file=sys.stderr)

    crawler = KeywordCrawler(
        start_url=args.start_url,
        user_agent=args.user_agent,
        max_pages=args.max_pages,
        delay=args.delay,
        concurrency=args.concurrency,
        output=args.output,
    )
    print(f"Starting crawl at {args.start_url} (domain: {crawler.base_domain})")
    print(f"Using User-Agent: {args.user_agent}")
    if crawler.robots.crawl_delay:
        print(f"Robots.txt crawl-delay detected: {crawler.robots.crawl_delay}s (enforcing min delay)")
    crawler.run()

if __name__ == "__main__":
    main()
