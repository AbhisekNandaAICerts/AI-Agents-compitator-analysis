# linkedin_playwright_crawler_auth_debug.py
# Save next to your project. Run `python linkedin_playwright_crawler_auth_debug.py --auth` to create auth.json,
# then run `python linkedin_playwright_crawler_auth_debug.py` to crawl.

import sys
# Windows Proactor policy to avoid NotImplementedError for Playwright subprocesses
if sys.platform.startswith("win"):
    import asyncio
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

import argparse
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

from playwright.async_api import async_playwright, BrowserContext

# config
COMPANY_SLUG = "udacity"                # change as needed
AUTH_JSON = Path("auth.json")
MAX_POSTS = 20
HEADLESS_BY_DEFAULT = True              # if auth creation -> headful; crawl -> headless by default
DEBUG_DIR = Path("debug_html"); DEBUG_DIR.mkdir(exist_ok=True)
OUTPUT_FILE = Path("crawl_results.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("linkedin_crawler_debug")

# selectors we will try
POST_SELECTORS = "div[role='article'], article, div.occludable-update, div[data-urn]"

async def create_auth_state(headless: bool = False):
    """Open a visible browser to let user login manually, then save storage state to AUTH_JSON."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=["--no-sandbox"])
        context = await browser.new_context()
        page = await context.new_page()
        logger.info("Opening LinkedIn login page for manual login. Please sign in in the opened browser.")
        await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=60000)
        # wait for user to complete login - instruct them
        print("\n--- Manual login: Please log into LinkedIn in the opened browser. ---")
        print("When you've completed login (and any MFA), come back here and press ENTER to continue.")
        input("Press ENTER after logging in (or close the browser to cancel)...")
        # try to detect logged-in state
        try:
            await page.wait_for_selector("header, nav #global-nav", timeout=10000)
            logger.info("Login appears successful (nav/header detected). Saving auth state to %s", AUTH_JSON)
        except Exception:
            logger.warning("Couldn't detect nav/header automatically; saving auth state anyway.")
        await context.storage_state(path=str(AUTH_JSON))
        await context.close()
        await browser.close()
        logger.info("Auth state saved to %s", AUTH_JSON)

async def crawl_with_auth(max_posts: int = MAX_POSTS, headless: bool = HEADLESS_BY_DEFAULT) -> List[Dict[str, Any]]:
    """Main crawl: uses AUTH_JSON to open an authenticated context and collect posts."""
    if not AUTH_JSON.exists():
        raise RuntimeError(f"{AUTH_JSON} not found. Run with --auth first to create storage state.")

    results = []
    semaphore = asyncio.Semaphore(4)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=["--no-sandbox"])
        # load auth storage state
        context = await browser.new_context(storage_state=str(AUTH_JSON))
        page = await context.new_page()

        # route handler: only block obvious analytics during debug; we keep CSS/JS to ensure rendering
        async def route_handler(route, request):
            url = request.url.lower()
            if any(x in url for x in ("google-analytics", "googletagmanager", "doubleclick", "facebook.net", "analytics")):
                await route.abort()
                return
            await route.continue_()
        try:
            await context.route("**/*", route_handler)
        except Exception:
            logger.warning("Failed to attach route handler; proceeding without request blocking.")

        posts_url = f"https://www.linkedin.com/company/{COMPANY_SLUG}/posts/"
        logger.info("Opening %s", posts_url)
        await page.goto(posts_url, wait_until="domcontentloaded", timeout=45000)
        try:
            # wait short time for XHRs; networkidle is okay to try but may be discouraged for tests
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            logger.info("networkidle wait timed out; proceeding to wait for post selector")

        # wait for at least one post-like selector
        try:
            await page.wait_for_selector(POST_SELECTORS, timeout=20000)
        except Exception:
            # save debug artifacts and exit early
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            html_path = DEBUG_DIR / f"no_posts_{COMPANY_SLUG}_{ts}.html"
            png_path = DEBUG_DIR / f"no_posts_{COMPANY_SLUG}_{ts}.png"
            try:
                content = await page.content()
                html_path.write_text(content, encoding="utf-8")
                await page.screenshot(path=str(png_path), full_page=True)
                logger.warning("No post-like selectors found. Saved debug HTML -> %s and screenshot -> %s", html_path, png_path)
            except Exception as e:
                logger.exception("Failed saving debug artifacts: %s", e)
            await context.close(); await browser.close()
            return []

        # collect articles and extract UID using same heuristics as your helpers
        collected = []
        seen = set()
        scroll_tries = 0
        while len(collected) < max_posts and scroll_tries < 60:
            candidates = await page.query_selector_all(POST_SELECTORS)
            logger.info("Found %d candidate nodes (scan %d)", len(candidates), scroll_tries)
            for c in candidates:
                try:
                    data_urn = await c.get_attribute("data-urn")
                    href_el = await c.query_selector("a[href*='/activity/'], a[href*='/posts/']")
                    href = (await href_el.get_attribute("href")) if href_el else None
                    # extract uid heuristics
                    uid = None
                    if data_urn:
                        import re
                        m = re.search(r"activity:(\d+)", data_urn)
                        if m: uid = m.group(1)
                    if not uid and href:
                        import re
                        m = re.search(r"/activity/(\d+)", href)
                        if m: uid = m.group(1)
                        else:
                            m2 = re.search(r"/posts/([^/?#]+)", href)
                            if m2: uid = m2.group(1)
                    # fallback: check any anchor inside
                    if not uid:
                        anchors = await c.query_selector_all("a[href]")
                        for a in anchors:
                            ah = await a.get_attribute("href")
                            if ah:
                                import re
                                m = re.search(r"/activity/(\d+)", ah)
                                if m:
                                    uid = m.group(1); href = ah; break
                                m2 = re.search(r"/posts/([^/?#]+)", ah)
                                if m2:
                                    uid = m2.group(1); href = ah; break
                    if uid and uid not in seen:
                        seen.add(uid)
                        snippet = (await c.inner_text())[:500]
                        collected.append({"uid": uid, "href": href or posts_url, "snippet": snippet})
                        logger.info("Collected uid=%s (total=%d)", uid, len(collected))
                        if len(collected) >= max_posts:
                            break
                except Exception:
                    continue
            # if none collected but candidates exist, dump first candidate outerHTML for tuning
            if not collected and candidates:
                try:
                    outer = await candidates[0].evaluate("e => e.outerHTML")
                    sample = DEBUG_DIR / f"sample_article_{COMPANY_SLUG}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.html"
                    sample.write_text(outer, encoding="utf-8")
                    logger.warning("Saved sample article outerHTML to %s for selector tuning", sample)
                except Exception:
                    pass
            # scroll to load more posts
            await page.evaluate("window.scrollBy(0, window.innerHeight);")
            await page.wait_for_timeout(800)
            scroll_tries += 1

        logger.info("Collected %d posts to process", len(collected))
        if not collected:
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            html_path = DEBUG_DIR / f"empty_after_scroll_{COMPANY_SLUG}_{ts}.html"
            png_path = DEBUG_DIR / f"empty_after_scroll_{COMPANY_SLUG}_{ts}.png"
            try:
                content = await page.content()
                html_path.write_text(content, encoding="utf-8")
                await page.screenshot(path=str(png_path), full_page=True)
                logger.warning("After scrolling, no uids collected. Saved HTML->%s, PNG->%s", html_path, png_path)
            except Exception:
                pass
            await context.close(); await browser.close()
            return []

        # Minimal per-post extraction (open permalink and extract text + images) - sequential for simplicity/debug
        out = []
        for item in collected:
            uid = item["uid"]
            href = item["href"]
            logger.info("Opening post %s -> %s", uid, href)
            try:
                post_page = await context.new_page()
                await post_page.goto(href, wait_until="domcontentloaded", timeout=30000)
                try:
                    await post_page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                article = await post_page.query_selector("div[role='article'], article")
                text = ""
                images = []
                if article:
                    try:
                        # prefer common text containers
                        txt_nodes = await article.query_selector_all("div.feed-shared-update__description, div.feed-shared-text, p")
                        if txt_nodes:
                            parts = []
                            for n in txt_nodes:
                                t = (await n.inner_text()).strip()
                                if t: parts.append(t)
                            text = "\n\n".join(parts)
                        else:
                            text = (await article.inner_text())[:5000]
                        imgs = await article.query_selector_all("img")
                        for im in imgs:
                            s = await im.get_attribute("src")
                            if s and s.startswith("http"):
                                images.append(s)
                    except Exception:
                        pass
                post_obj = {"uid": uid, "href": href, "text": text, "images": images}
                out.append(post_obj)
                await post_page.close()
            except Exception as e:
                logger.exception("Error fetching post page for uid=%s: %s", uid, e)

        await context.close(); await browser.close()
        return out

def run_sync(auth_mode: bool = False):
    if auth_mode:
        # create auth
        asyncio.run(create_auth_state(headless=False))
        print("Saved auth.json. Now run crawler normally: python linkedin_playwright_crawler_auth_debug.py")
        return
    # run crawl
    res = asyncio.run(crawl_with_auth(max_posts=MAX_POSTS, headless=HEADLESS_BY_DEFAULT))
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(res or [], f, ensure_ascii=False, indent=2)
    print(f"Saved {len(res or [])} results to {OUTPUT_FILE}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--auth", action="store_true", help="Open browser for manual LinkedIn login and save auth.json")
    args = parser.parse_args()
    run_sync(auth_mode=args.auth)
