#!/usr/bin/env python3
"""
sitemap_product_course_filter_fast.py

Quick mode: Uses robots.txt + sitemaps and ONLY filters by keywords in URL paths.
No heavy HTML-content checking or large page fetch loops.

Requirements:
    pip install requests
"""

import time
import requests
import urllib.robotparser
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse
from typing import List

# --- Config / defaults ---
USER_AGENT = "Mozilla/5.0 (compatible; sitemap-filter-bot/1.0)"
REQUEST_TIMEOUT = 10
SLEEP_BETWEEN_REQUESTS = 0.25  # polite pacing
DEFAULT_KEYWORDS = [
    "product", "products",
    "course", "courses",
    "cert", "certificate", "certification",
    "training", "certified"
]


# --- helpers ---
def fetch_text(url: str) -> str:
    try:
        headers = {"User-Agent": USER_AGENT}
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except Exception:
        return ""


def get_robots_txt(base_url: str) -> str:
    if not base_url.endswith("/"):
        base_url += "/"
    robots_url = urljoin(base_url, "robots.txt")
    return fetch_text(robots_url)


def get_sitemaps_from_robots(robots_txt: str) -> List[str]:
    sitemaps = []
    for line in robots_txt.splitlines():
        if line.strip().lower().startswith("sitemap:"):
            sitemaps.append(line.split(":", 1)[1].strip())
    return sitemaps


def parse_sitemap(sitemap_url: str, seen=None) -> List[str]:
    if seen is None:
        seen = set()
    urls = []
    if sitemap_url in seen:
        return urls
    seen.add(sitemap_url)

    try:
        headers = {"User-Agent": USER_AGENT}
        resp = requests.get(sitemap_url, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
        print(f"[!] Failed to download/parse sitemap {sitemap_url}: {e}")
        return urls

    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

    for loc in root.findall(".//sm:url/sm:loc", ns):
        if loc.text:
            urls.append(loc.text.strip())

    for s in root.findall(".//sm:sitemap/sm:loc", ns):
        if s.text:
            nested = s.text.strip()
            urls.extend(parse_sitemap(nested, seen=seen))
    return urls


def build_robot_parser_from_text(robots_txt: str, base_url: str) -> urllib.robotparser.RobotFileParser:
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(robots_txt.splitlines())
    try:
        rp.set_url(urljoin(base_url if base_url.endswith("/") else base_url + "/", "robots.txt"))
    except Exception:
        pass
    return rp


def filter_allowed_urls_by_robots(urls: List[str], rp: urllib.robotparser.RobotFileParser) -> List[str]:
    allowed = []
    for u in urls:
        try:
            if rp.can_fetch(USER_AGENT, u) or rp.can_fetch("*", u):
                allowed.append(u)
        except Exception:
            continue
    return allowed


def url_matches_keywords(url: str, keywords: List[str]) -> bool:
    lower = url.lower()
    return any(kw in lower for kw in keywords)


# --- main flow (fast only) ---
def collect_crawlable_relevant_links_fast(
    site_url: str,
    keywords: List[str] = None,
) -> List[str]:
    if keywords is None:
        keywords = DEFAULT_KEYWORDS

    parsed = urlparse(site_url)
    if not parsed.scheme:
        site_url = "https://" + site_url
    base_url = f"{urlparse(site_url).scheme}://{urlparse(site_url).netloc}"

    print(f"Base site: {base_url}")

    robots_txt = get_robots_txt(base_url)
    if not robots_txt:
        print("❌ No robots.txt found or failed to fetch. Aborting.")
        return []

    sitemaps = get_sitemaps_from_robots(robots_txt)
    if not sitemaps:
        print("❌ No sitemap entries found in robots.txt. Aborting.")
        return []

    print(f"Found {len(sitemaps)} sitemap(s) in robots.txt.")
    candidate_urls = []
    for sm in sitemaps:
        print(f"Parsing sitemap: {sm}")
        candidate_urls.extend(parse_sitemap(sm))
        time.sleep(SLEEP_BETWEEN_REQUESTS)

    candidate_urls = list(dict.fromkeys(candidate_urls))
    print(f"Total URLs found in sitemaps: {len(candidate_urls)}")

    rp = build_robot_parser_from_text(robots_txt, base_url)
    allowed_urls = filter_allowed_urls_by_robots(candidate_urls, rp)
    print(f"Allowed by robots.txt: {len(allowed_urls)}")

    # FAST filter: only match keywords in URL path (no page fetching)
    fast_matched = [u for u in allowed_urls if url_matches_keywords(u, keywords)]
    print(f"Fast URL-match results: {len(fast_matched)}")

    return sorted(fast_matched)


def main():
    site = input("Enter company URL (e.g. https://www.example.com): ").strip()
    print("Press Enter to use default keywords or enter comma-separated keywords.")
    kw_input = input(f"Default keywords: {', '.join(DEFAULT_KEYWORDS)}\n> ").strip()
    keywords = None
    if kw_input:
        keywords = [k.strip().lower() for k in kw_input.split(",") if k.strip()]
    print("\nRunning fast mode... (no HTML content checks)\n")
    results = collect_crawlable_relevant_links_fast(site, keywords=keywords)
    print("\n=== Relevant crawlable links (fast mode) ===")
    if results:
        for r in results:
            print(r)
    else:
        print("No relevant links found using the provided filters.")


if __name__ == "__main__":
    main()
