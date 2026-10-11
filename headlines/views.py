from django.shortcuts import render
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_GET
from django.utils.html import escape
import requests
import feedparser
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import time
import re
from datetime import datetime

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ----------------------------------------------------------------------
# CACHE
# ----------------------------------------------------------------------
_CACHE = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 600
_REFRESH_IN_PROGRESS = False


def _get_cache():
    with _CACHE_LOCK:
        return dict(_CACHE)


def _set_cache(data):
    with _CACHE_LOCK:
        _CACHE.clear()
        _CACHE.update(data)


def _is_stale():
    with _CACHE_LOCK:
        if not _CACHE:
            return True
        return (time.time() - _CACHE.get("updated", 0)) > _CACHE_TTL


# ----------------------------------------------------------------------
# Basic fetchers
# ----------------------------------------------------------------------
def fetch_html(url, timeout=12):
    try:
        r = requests.get(url, headers=BASE_HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.text
    except requests.RequestException as exc:
        print(f"[fetch] failed {url}: {exc}")
        return None


def _clean_text(text, limit=200):
    if not text:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    clean = soup.get_text(" ", strip=True)
    return (clean[:limit] + "...") if len(clean) > limit else clean


# ----------------------------------------------------------------------
# og: meta tag fallback
# ----------------------------------------------------------------------
def fetch_og_tags(url):
    html = fetch_html(url, timeout=10)
    if not html:
        return "", "", ""
    soup = BeautifulSoup(html, "html.parser")

    def meta(prop):
        tag = (
            soup.find("meta", property=prop)
            or soup.find("meta", attrs={"name": prop})
        )
        return tag.get("content", "").strip() if tag else ""

    image = meta("og:image") or meta("twitter:image")
    desc = (
        meta("og:description")
        or meta("description")
        or meta("twitter:description")
    )

    if image and not image.startswith(("http", "//")):
        image = urljoin(url, image)
    if image.startswith("//"):
        image = "https:" + image

    return image, _clean_text(desc), meta("og:title")


def _enrich_one(art):
    if art["image"] and art["summary"]:
        return art
    try:
        img, desc, _ = fetch_og_tags(art["link"])
        if not art["image"] and img:
            art["image"] = img
        if not art["summary"] and desc:
            art["summary"] = desc
    except Exception as exc:
        print(f"[enrich] failed {art['link']}: {exc}")
    return art


def enrich_articles_parallel(articles, max_workers=8):
    to_enrich = [a for a in articles if not (a["image"] and a["summary"])]
    if not to_enrich:
        return articles
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_enrich_one, a) for a in to_enrich]
        for _ in as_completed(futures):
            pass
    return articles


# ----------------------------------------------------------------------
# Image + summary extraction from list-page HTML
# ----------------------------------------------------------------------
IMG_ATTRS = ["src", "data-src", "data-lazy-src", "data-original",
             "data-srcset", "srcset"]


def _pick_image(node, base_url):
    if node is None:
        return ""
    for img in node.find_all("img"):
        for attr in IMG_ATTRS:
            val = img.get(attr)
            if not val or val.startswith("data:"):
                continue
            if attr in ("srcset", "data-srcset") and "," in val:
                val = val.split(",")[0].strip().split(" ")[0]
            if val.startswith("//"):
                val = "https:" + val
            elif not val.startswith("http"):
                val = urljoin(base_url, val)
            return val
    for el in node.find_all(style=True):
        m = re.search(r'url\(["\']?(.*?)["\']?\)', el["style"])
        if m:
            val = m.group(1)
            if val.startswith("//"):
                val = "https:" + val
            elif not val.startswith("http"):
                val = urljoin(base_url, val)
            return val
    return ""


SUMMARY_SELECTORS = [
    "p.summary", "p.excerpt", "p.description", "p.entry-summary",
    "div.summary", "div.excerpt", "div.description",
    ".story__excerpt", ".story__summary",
    ".fc-item__standfirst", ".fc-item__summary",
    ".media-body p", ".media-body", "p",
]


def _pick_summary(node):
    if node is None:
        return ""
    for sel in SUMMARY_SELECTORS:
        el = node.select_one(sel)
        if el:
            text = _clean_text(el.get_text(" ", strip=True))
            if len(text) > 20:
                return text
    return ""


# ----------------------------------------------------------------------
# List-page parser
# ----------------------------------------------------------------------
def parse_article_links(html, selectors, base_url, limit=40):
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    articles = []
    seen = set()

    for selector in selectors:
        for element in soup.select(selector):
            anchor = element if element.name == "a" else element.find("a")
            if not anchor:
                continue
            title = anchor.get_text(" ", strip=True)
            href = anchor.get("href", "").strip()
            if not title or not href or len(title) < 10:
                continue
            link = urljoin(base_url, href)
            if link in seen:
                continue
            seen.add(link)

            container = anchor
            for _ in range(5):
                if container.parent is None:
                    break
                container = container.parent
                if container.find("img") and container.find("p"):
                    break

            articles.append({
                "title": title,
                "link": link,
                "image": _pick_image(container, base_url),
                "summary": _pick_summary(container),
            })
            if len(articles) >= limit:
                return articles
    return articles


# ----------------------------------------------------------------------
# RSS
# ----------------------------------------------------------------------
def _extract_image_from_entry(entry):
    media = entry.get("media_content") or []
    if media and isinstance(media, list) and media[0].get("url"):
        return media[0]["url"]
    thumbs = entry.get("media_thumbnail") or []
    if thumbs and isinstance(thumbs, list) and thumbs[0].get("url"):
        return thumbs[0]["url"]
    for enc in (entry.get("enclosures") or []):
        if enc.get("type", "").startswith("image"):
            return enc.get("href") or enc.get("url")
    summary = entry.get("summary", "") or entry.get("description", "")
    if summary:
        soup = BeautifulSoup(summary, "html.parser")
        img = soup.find("img")
        if img and img.get("src"):
            return img["src"]
    return ""


def fetch_rss(feed_urls, limit=40):
    for feed_url in feed_urls:
        try:
            r = requests.get(feed_url, headers=BASE_HEADERS, timeout=12)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
            articles = []
            seen = set()
            for entry in feed.entries:
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                if not title or not link or link in seen:
                    continue
                seen.add(link)
                articles.append({
                    "title": title,
                    "link": link,
                    "image": _extract_image_from_entry(entry),
                    "summary": _clean_text(
                        entry.get("summary", "") or entry.get("description", "")
                    ),
                })
                if len(articles) >= limit:
                    break
            if articles:
                print(f"[rss] {len(articles)} from {feed_url}")
                return articles
        except Exception as exc:
            print(f"[rss] failed {feed_url}: {exc}")
    return []


# ----------------------------------------------------------------------
# Per-paper scrapers
# ----------------------------------------------------------------------
def get_tribune_headlines():
    articles = fetch_rss([
        "https://tribune.com.pk/feed/home",
        "https://tribune.com.pk/pakistan/feed/",
        "https://tribune.com.pk/feed/pakistan",
    ])
    if not articles:
        html = fetch_html("https://tribune.com.pk/")
        articles = parse_article_links(
            html,
            ["a[href*='/story/']", "h2 a[href]", "h3 a[href]", "article a[href]"],
            "https://tribune.com.pk/",
        )
    return enrich_articles_parallel(articles)


def get_daily_headlines():
    articles = fetch_rss(["https://dailytimes.com.pk/feed/"])
    if not articles:
        html = fetch_html("https://dailytimes.com.pk/")
        articles = parse_article_links(
            html,
            ["h1.entry-title a", "h2.entry-title a", "h3.entry-title a",
             "h4.entry-title a", "article a[href]",
             ".jeg_post_title a", ".entry-title a"],
            "https://dailytimes.com.pk/",
        )
    return enrich_articles_parallel(articles)


def get_dawn_headlines():
    html = fetch_html("https://www.dawn.com/")
    articles = parse_article_links(
        html,
        ["h2.story__title a", "h2 a[href]", "h3 a[href]"],
        "https://www.dawn.com/",
    )
    return enrich_articles_parallel(articles)


def get_news_headlines():
    html = fetch_html("https://www.thenews.com.pk/today")
    articles = parse_article_links(
        html,
        ["li.print-list__item h2 a", "h2.fc-item__title a",
         "h2 a[href]", "h3 a[href]"],
        "https://www.thenews.com.pk/",
    )
    return enrich_articles_parallel(articles)


def get_nation_headlines():
    html = fetch_html("https://www.nation.com.pk/Todays-Paper")
    articles = parse_article_links(
        html,
        ["article.jeg_post h3.jeg_post_title a", "h3.jeg_post_title a",
         "article h2 a[href]", "article h3 a[href]"],
        "https://www.nation.com.pk/",
    )
    return enrich_articles_parallel(articles)


# ----------------------------------------------------------------------
# Background refresh
# ----------------------------------------------------------------------
PAPER_MAP = {
    "the_news":    {"name": "The News",     "fetcher": get_news_headlines},
    "tribune":     {"name": "Tribune",      "fetcher": get_tribune_headlines},
    "daily_times": {"name": "Daily Times",  "fetcher": get_daily_headlines},
    "nation":      {"name": "Nation",       "fetcher": get_nation_headlines},
}


def _scrape_all():
    global _REFRESH_IN_PROGRESS

    def one(key, meta):
        try:
            print(f"[scrape] fetching {key} …")
            arts = meta["fetcher"]()
            print(f"[scrape] {key} → {len(arts)} articles")
            return key, {"name": meta["name"], "articles": arts}
        except Exception as exc:
            print(f"[scrape] {key} failed: {exc}")
            return key, {"name": meta["name"], "articles": []}

    results = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(one, k, m) for k, m in PAPER_MAP.items()]
        for fut in as_completed(futures):
            key, data = fut.result()
            results[key] = data

    _set_cache({"papers": results, "updated": time.time()})
    print("[scrape] cache updated at", time.strftime("%H:%M:%S"))

    with _CACHE_LOCK:
        _REFRESH_IN_PROGRESS = False


def _trigger_refresh_if_needed(blocking=False):
    global _REFRESH_IN_PROGRESS
    with _CACHE_LOCK:
        if _REFRESH_IN_PROGRESS and not blocking:
            return
        fresh = _CACHE and (time.time() - _CACHE.get("updated", 0)) < _CACHE_TTL
        if fresh:
            return
        _REFRESH_IN_PROGRESS = True

    if blocking:
        _scrape_all()
    else:
        threading.Thread(target=_scrape_all, daemon=True).start()


# ----------------------------------------------------------------------
# Views
# ----------------------------------------------------------------------
def home_view(request):
    cache = _get_cache()
    if not cache.get("papers"):
        _trigger_refresh_if_needed(blocking=True)
        cache = _get_cache()
    else:
        _trigger_refresh_if_needed(blocking=False)

    papers = cache.get("papers", {})

    latest = {}
    for key, meta in PAPER_MAP.items():
        entry = papers.get(key, {"name": meta["name"], "articles": []})
        latest[key] = {
            "name": entry["name"],
            "articles": entry["articles"][:5],
        }

    return render(request, "home.html", {
        "latest": latest,
        "loading": not papers,
    })


@require_GET
def daily_paper_json(request, paper):
    """Return the full article list for a given paper as JSON."""
    cache = _get_cache()
    papers = cache.get("papers", {})
    if paper not in papers:
        return JsonResponse({"articles": [], "error": "unknown paper"}, status=404)
    return JsonResponse({
        "name": papers[paper]["name"],
        "articles": papers[paper]["articles"],
    })


def force_refresh(request):
    with _CACHE_LOCK:
        _CACHE.clear()
    _trigger_refresh_if_needed(blocking=True)
    return JsonResponse({"status": "refreshed"})


def dawn_view(request):
    _trigger_refresh_if_needed(blocking=not _get_cache().get("papers"))
    articles = _get_cache().get("papers", {}).get("dawn", {}).get("articles", [])
    return render(request, "dawn.html", {"newspaper": "Dawn", "headlines": articles})


def thenews_view(request):
    _trigger_refresh_if_needed(blocking=not _get_cache().get("papers"))
    articles = _get_cache().get("papers", {}).get("the_news", {}).get("articles", [])
    return render(request, "thenews.html", {"newspaper": "The News", "headlines": articles})


def thenation_view(request):
    _trigger_refresh_if_needed(blocking=not _get_cache().get("papers"))
    articles = _get_cache().get("papers", {}).get("nation", {}).get("articles", [])
    return render(request, "thenation.html", {"newspaper": "The Nation", "headlines": articles})


def dailytimes_view(request):
    _trigger_refresh_if_needed(blocking=not _get_cache().get("papers"))
    articles = _get_cache().get("papers", {}).get("daily_times", {}).get("articles", [])
    return render(request, "dailytimes.html", {"newspaper": "Daily Times", "headlines": articles})


def tribune_view(request):
    _trigger_refresh_if_needed(blocking=not _get_cache().get("papers"))
    articles = _get_cache().get("papers", {}).get("tribune", {}).get("articles", [])
    return render(request, "tribune.html", {"newspaper": "Express Tribune", "headlines": articles})


# ----------------------------------------------------------------------
# SEO: Sitemap, Robots, ads.txt, Article detail
# ----------------------------------------------------------------------
@require_GET
def sitemap_xml(request):
    cache = _get_cache()
    papers = cache.get("papers", {})
    now = datetime.utcnow()
    site_url = request.build_absolute_uri("/").rstrip("/")

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"',
        '        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">',
    ]

    for key, block in papers.items():
        for art in block.get("articles", []):
            title = escape(art.get("title", ""))
            link = art.get("link", "")
            if not title or not link:
                continue
            pub_date = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            parts.append("  <url>")
            parts.append(f"    <loc>{escape(link)}</loc>")
            parts.append("    <news:news>")
            parts.append("      <news:publication>")
            parts.append("        <news:name>NewsHub</news:name>")
            parts.append("        <news:language>en</news:language>")
            parts.append("      </news:publication>")
            parts.append(f"      <news:publication_date>{pub_date}</news:publication_date>")
            parts.append(f"      <news:title>{title}</news:title>")
            parts.append("    </news:news>")
            parts.append("  </url>")

    parts.append("</urlset>")
    return HttpResponse("\n".join(parts), content_type="application/xml")


@require_GET
def robots_txt(request):
    site_url = request.build_absolute_uri("/").rstrip("/")
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /admin/",
        "Disallow: /refresh/",
        "",
        f"Sitemap: {site_url}/sitemap.xml",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")


@require_GET
def ads_txt(request):
    lines = [
        "google.com, pub-3802548798997279, DIRECT, f08c47fec0942fa0",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")


def article_detail(request):
    return render(request, "article.html", {
        "title": request.GET.get("title", ""),
        "url": request.GET.get("url", ""),
        "image": request.GET.get("image", ""),
        "summary": request.GET.get("summary", ""),
    })