from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib import error, request
from urllib.parse import parse_qs, quote, quote_plus, urlencode, unquote, urljoin, urlparse, urlunparse
from xml.etree import ElementTree


ReviewReport = Dict[str, Any]
ReviewSource = Dict[str, Any]
SearchMeta = Dict[str, Any]

SEARCH_RESULT_LIMIT = 12
SEARCH_TIMEOUT_SECONDS = 20
RSS_CANDIDATE_LIMIT = 36
MIN_FREE_RESULTS_BEFORE_LEGACY_FALLBACK = 4
MAX_SEARCH_RESPONSE_BYTES = 1_500_000


INCENTIVE_KEYWORDS = [
    "五星送",
    "5星送",
    "五顆星送",
    "評論送",
    "評價送",
    "打卡送",
    "按讚送",
    "好評送",
    "送飲料",
    "送小菜",
    "送甜點",
]
SPONSORED_KEYWORDS = ["業配", "合作", "邀約", "試吃邀約", "店家邀請", "本文與", "贊助"]
SHORT_PRAISE = ["好吃", "讚", "服務好", "很棒", "推", "推薦", "五星", "滿分"]
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
SOCIAL_MEDIA_HOSTS = {
    "facebook.com", "www.facebook.com", "m.facebook.com",
    "instagram.com", "www.instagram.com", "threads.net", "www.threads.net",
}
SEARCH_PROVIDER_NAMES = {
    "google_maps", "ifoodie", "bing_rss", "google_news_rss", "multi", "playwright", "http",
    "known_url", "user_url", "legacy",
}
RESTAURANT_NAME_ALIASES = {
    "大甲麥當勞": ["麥當勞 S216 大甲經國店", "麥當勞 大甲經國店"],
    "奔頂牛排東海": ["犇頂牛排 PLUS+ 東海店", "犇頂牛排 東海店"],
    "忠鼎原汁牛肉麵": ["忠鼎原汁牛肉麵 西屯", "忠鼎牛肉麵 東海"],
    "愛將平價牛排東海店": ["愛將平價牛排 台中東海店", "愛將平價牛排 東海店"],
    "愛將東海": ["愛將平價牛排 台中東海店", "愛將平價牛排 東海店"],
}
KNOWN_REVIEW_URLS = {
    "大甲麥當勞": [
        "https://www.foodpanda.com.tw/restaurant/b4kp/mai-dang-lao-s216-da-jia-jing-guo-dian/reviews",
    ],
    "奔頂牛排東海": [
        "https://ifoodie.tw/restaurant/652a96d90fbeb1b8d6a818e2-%E7%8A%87%E9%A0%82%E7%89%9B%E6%8E%92-%E6%9D%B1%E6%B5%B7%E5%BA%97",
    ],
    "愛將東海": [
        "https://www.foodpanda.com.tw/restaurant/e8je/ai-jiang-ping-jia-niu-pai-tai-zhong-dong-hai-dian/reviews",
    ],
    "愛將平價牛排東海店": [
        "https://www.foodpanda.com.tw/restaurant/e8je/ai-jiang-ping-jia-niu-pai-tai-zhong-dong-hai-dian/reviews",
    ],
    "森森燒肉台中中科店": [
        "https://ifoodie.tw/restaurant/5c2cd8be23679c54eee6efd6-%E6%A3%AE%E6%A3%AE%E7%87%92%E8%82%89MoriMoriYakiniku",
    ],
}


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().strip(".")
    return cleaned or "unknown_restaurant"


def _cache_path(project_root: str | Path, restaurant_name: str) -> Path:
    return Path(project_root) / f"reviews_{_safe_filename(restaurant_name)}.json"


async def _collect_review_sources(restaurant_name: str) -> List[ReviewSource]:
    queries = [
        f"{restaurant_name} 評價",
        f"{restaurant_name} 部落格 評價",
        f"{restaurant_name} dcard ptt google 評價",
    ]
    rows, _ = await _search_public_web(queries, relevance_terms=[restaurant_name])
    return rows


def _empty_search_meta(status: str = "no_relevant_sources") -> SearchMeta:
    return {
        "status": status,
        "provider": None,
        "attemptedProviders": [],
        "rawResultCount": 0,
        "relevantSourceCount": 0,
    }


def _search_status_message(status: str) -> str:
    return {
        "blocked": "搜尋頁要求驗證，暫時無法取得公開來源",
        "browser_unavailable": "搜尋瀏覽器尚未安裝，且其他來源沒有結果",
        "network_error": "連線到公開搜尋服務失敗，請稍後再試",
        "no_relevant_sources": "搜尋完成，但沒有找到可確認為此分店的公開評價",
    }.get(status, "找不到足夠的公開評價來源")


def _append_unique_search_rows(
    target: List[ReviewSource],
    incoming: List[ReviewSource],
    seen_urls: set[str],
    provider: str,
    limit: int = SEARCH_RESULT_LIMIT,
    allow_search_urls: bool = False,
) -> int:
    raw_count = len(incoming)
    for rank, row in enumerate(incoming, start=1):
        url = _canonicalize_url(str(row.get("url") or ""))
        title = re.sub(r"\s+", " ", str(row.get("title") or "")).strip()
        excerpt = re.sub(
            r"\s+", " ", str(row.get("excerpt") or row.get("snippet") or "")
        ).strip()
        if (
            not url
            or url in seen_urls
            or _is_youtube_url(url)
            or (not allow_search_urls and _is_search_noise_url(url))
            or not title
        ):
            continue
        seen_urls.add(url)
        target.append(
            {
                "title": title[:160],
                "url": url,
                "excerpt": (excerpt or title)[:700],
                "sourceType": _classify_source(url, title),
                "provider": provider,
                "rank": rank,
                "publishedAt": row.get("publishedAt"),
                "platformRating": row.get("platformRating"),
            }
        )
        if len(target) >= limit:
            break
    return raw_count


class SearchProviderError(RuntimeError):
    def __init__(self, status: str, message: str = "") -> None:
        super().__init__(message or status)
        self.status = status


def _strip_html_text(value: str) -> str:
    value = re.sub(r"<script\b[^>]*>.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style\b[^>]*>.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", unescape(value)).strip()


def _rss_date(value: str) -> Optional[str]:
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except Exception:
        return _parse_date(value)


def _parse_rss_search_results(xml_text: str, provider: str) -> List[ReviewSource]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise SearchProviderError("network_error", "invalid RSS response") from exc

    rows: List[ReviewSource] = []
    for item in root.findall(".//item"):
        title = _strip_html_text(item.findtext("title") or "")
        url = _clean_search_url(item.findtext("link") or "")
        excerpt = _strip_html_text(item.findtext("description") or "")
        if not title or not url:
            continue
        rows.append(
            {
                "title": title[:160],
                "url": url,
                "excerpt": (excerpt or title)[:700],
                "sourceType": "news" if provider == "google_news_rss" else _classify_source(url, title),
                "publishedAt": _rss_date(item.findtext("pubDate") or ""),
            }
        )
    return rows


def _restaurant_search_names(identity: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    for value in [identity.get("officialName"), identity.get("queryName")]:
        name = str(value or "").replace("_", " ").strip()
        if name and name not in names:
            names.append(name)
    compact_names = {re.sub(r"\W+", "", name).lower() for name in names}
    for key, aliases in RESTAURANT_NAME_ALIASES.items():
        normalized_key = re.sub(r"\W+", "", key).lower()
        if normalized_key not in compact_names:
            continue
        for alias in aliases:
            if alias not in names:
                names.append(alias)
    return names


def _search_row_matches_terms(row: ReviewSource, relevance_terms: Optional[List[str]]) -> bool:
    if not relevance_terms:
        return True
    haystack = re.sub(
        r"\W+",
        "",
        f"{row.get('title', '')} {row.get('excerpt', '')}",
    ).lower()
    for value in relevance_terms:
        text = re.sub(r"[\"'（）()\[\]【】]", " ", str(value)).replace("_", " ")
        text = re.sub(
            r"\b(?:評價|食記|評論|部落格|美食|地址|分店|Google|Dcard|PTT)\b",
            " ",
            text,
            flags=re.I,
        )
        parts = [
            re.sub(r"\W+", "", part).lower()
            for part in re.split(r"[\s－—|｜]+", text)
            if re.sub(r"\W+", "", part)
        ]
        compact = re.sub(r"\W+", "", text).lower()
        if compact and compact in haystack:
            return True
        if len(parts) >= 2:
            branch_variants = {
                parts[-1],
                re.sub(r"店$", "", parts[-1]),
                re.sub(r"^(?:台|臺)[北中南東]", "", parts[-1]),
                re.sub(r"店$", "", re.sub(r"^(?:台|臺)[北中南東]", "", parts[-1])),
            }
            if parts[0] in haystack and any(
                len(branch) >= 2 and branch in haystack for branch in branch_variants
            ):
                return True
    return False


def _collect_review_sources_via_bing_rss(
    queries: List[str],
    diagnostics: Optional[List[str]] = None,
    limit: int = RSS_CANDIDATE_LIMIT,
) -> List[ReviewSource]:
    gathered: List[ReviewSource] = []
    seen: set[str] = set()
    for query in queries:
        url = (
            "https://www.bing.com/search?format=rss&cc=TW&mkt=zh-TW"
            f"&setlang=zh-Hant&q={quote_plus(query)}"
        )
        try:
            rows = _parse_rss_search_results(_fetch_search_html(url), "bing_rss")
        except Exception as exc:
            _record_http_search_error(diagnostics, exc)
            continue
        for row in rows:
            canonical = _canonicalize_url(str(row.get("url") or ""))
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            gathered.append(row)
            if len(gathered) >= limit:
                return gathered
    return gathered


def _collect_review_sources_via_google_news_rss(
    queries: List[str],
    diagnostics: Optional[List[str]] = None,
    limit: int = SEARCH_RESULT_LIMIT,
) -> List[ReviewSource]:
    gathered: List[ReviewSource] = []
    seen: set[str] = set()
    for query in queries[:2]:
        url = (
            f"https://news.google.com/rss/search?q={quote_plus(query)}"
            "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        )
        try:
            rows = _parse_rss_search_results(_fetch_search_html(url), "google_news_rss")
        except Exception as exc:
            _record_http_search_error(diagnostics, exc)
            continue
        for row in rows:
            canonical = _canonicalize_url(str(row.get("url") or ""))
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            gathered.append(row)
            if len(gathered) >= limit:
                return gathered
    return gathered


def _collect_review_sources_via_ifoodie(
    search_names: List[str],
    diagnostics: Optional[List[str]] = None,
    limit: int = SEARCH_RESULT_LIMIT,
) -> List[ReviewSource]:
    from bs4 import BeautifulSoup

    gathered: List[ReviewSource] = []
    seen: set[str] = set()
    for search_name in search_names[:3]:
        url = "https://ifoodie.tw/explore/list/" + quote(search_name, safe="")
        try:
            html = _fetch_search_html(url)
            soup = BeautifulSoup(html, "html.parser")
        except Exception as exc:
            _record_http_search_error(diagnostics, exc)
            continue

        for anchor in soup.select('a[href*="/restaurant/"]'):
            title = re.sub(r"\s+", " ", " ".join(anchor.stripped_strings)).strip()
            target_url = _canonicalize_url(urljoin(url, str(anchor.get("href") or "")))
            if (
                not title
                or title.startswith("(")
                or not target_url
                or target_url in seen
            ):
                continue
            excerpt = ""
            node = anchor
            for _ in range(4):
                node = node.parent
                if node is None:
                    break
                candidate = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
                if 30 <= len(candidate) <= 1200:
                    excerpt = candidate
                if len(candidate) > 1200:
                    break
            row = {
                "title": title[:160],
                "url": target_url,
                "excerpt": (excerpt or title)[:700],
                "sourceType": "review_platform",
            }
            if not _search_row_matches_terms(row, [search_name]):
                compact_title = re.sub(r"\W+", "", title).lower()
                first_name_part = re.sub(
                    r"\W+", "", re.split(r"[\s_－—|｜]+", search_name)[0]
                ).lower()
                if len(first_name_part) < 2 or first_name_part not in compact_title:
                    continue
            seen.add(target_url)
            gathered.append(row)
            if len(gathered) >= limit:
                return gathered
    return gathered


def _record_http_search_error(diagnostics: Optional[List[str]], exc: Exception) -> None:
    if diagnostics is None:
        return
    if isinstance(exc, error.HTTPError) and exc.code in {401, 403, 429}:
        diagnostics.append("blocked")
    elif isinstance(exc, (error.HTTPError, error.URLError, TimeoutError, OSError)):
        diagnostics.append("network_error")
    else:
        diagnostics.append("network_error")


def _parse_google_maps_profile(
    query_name: str,
    page_url: str,
    page_title: str,
    body_text: str,
    aria_labels: Optional[List[str]] = None,
) -> Optional[ReviewSource]:
    title = re.sub(r"\s*-\s*Google\s*(?:地圖|Maps)\s*$", "", page_title, flags=re.I).strip()
    body = re.sub(r"[\ue000-\uf8ff]", " ", body_text or "")
    body = re.sub(r"[ \t]+", " ", body)
    if not title or "找不到" in body[:500] or "Google Maps can't find" in body[:500]:
        return None

    rating: Optional[float] = None
    for label in aria_labels or []:
        match = re.search(r"([1-5](?:\.\d{1,2})?)\s*(?:顆星|stars?)", label, re.I)
        if match:
            rating = float(match.group(1))
            break
    if rating is None:
        match = re.search(r"(?m)^\s*([1-5](?:\.\d{1,2})?)\s*$", body)
        if match:
            rating = float(match.group(1))
    if rating is None:
        return None

    review_count: Optional[int] = None
    count_patterns = [
        r"([\d,]+)\+?\s*(?:則|篇)?\s*(?:Google\s*)?(?:評論|評價)",
        r"([\d,]+)\+?\s*(?:reviews?|ratings?)",
    ]
    count_haystack = " ".join(aria_labels or []) + " " + body[:1800]
    for pattern in count_patterns:
        match = re.search(pattern, count_haystack, re.I)
        if match:
            review_count = int(match.group(1).replace(",", ""))
            break

    address = _extract_address(body[:1800])
    place_name = title or query_name
    rating_text = f"Google 地圖公開頁顯示評分 {rating}/5"
    if review_count:
        rating_text += f"，共 {review_count} 則評論"
    if address:
        rating_text += f"；地址：{address}"
    return {
        "title": f"{place_name} | Google 地圖評價",
        "url": page_url,
        "excerpt": rating_text,
        "sourceType": "google",
        "provider": "google_maps",
        "platformRating": {
            "average": rating,
            "reviewCount": review_count,
            "platform": "Google Maps",
        },
    }


async def _playwright_google_maps_profile(
    restaurant_name: str,
    address: str = "",
) -> List[ReviewSource]:
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:
        raise SearchProviderError("browser_unavailable") from exc

    query = " ".join(part for part in [restaurant_name.strip(), address.strip()] if part)
    if not query:
        return []
    playwright_ctx = async_playwright()
    browser = None
    try:
        playwright = await playwright_ctx.__aenter__()
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(
            locale="zh-TW",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
            ),
        )
        maps_url = "https://www.google.com/maps/search/?api=1&query=" + quote_plus(query)
        await page.goto(
            maps_url,
            wait_until="domcontentloaded",
            timeout=SEARCH_TIMEOUT_SECONDS * 1000,
        )
        await page.wait_for_timeout(3000)
        if any(marker in page.url.lower() for marker in ("/sorry/", "captcha", "recaptcha")):
            raise SearchProviderError("blocked")
        body = await page.locator("body").inner_text(timeout=5000)
        labels = await page.locator("[aria-label]").evaluate_all(
            "elements => elements.map(element => element.getAttribute('aria-label') || '')"
        )
        row = _parse_google_maps_profile(
            restaurant_name,
            page.url,
            await page.title(),
            body[:8000],
            [str(label) for label in labels],
        )
        return [row] if row else []
    except SearchProviderError:
        raise
    except Exception as exc:
        raise SearchProviderError("network_error") from exc
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        try:
            await playwright_ctx.__aexit__(None, None, None)
        except Exception:
            pass


async def _playwright_search_queries(
    queries: List[str], limit: int = SEARCH_RESULT_LIMIT
) -> List[ReviewSource]:
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:
        raise SearchProviderError("browser_unavailable") from exc

    playwright_ctx = async_playwright()
    browser = None
    try:
        playwright = await playwright_ctx.__aenter__()
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(
            locale="zh-TW",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
            ),
        )
        gathered: List[ReviewSource] = []
        for query in queries:
            try:
                await page.goto(
                    f"https://www.google.com/search?hl=zh-TW&q={quote_plus(query)}",
                    wait_until="domcontentloaded",
                    timeout=SEARCH_TIMEOUT_SECONDS * 1000,
                )
                results = await page.evaluate(
                    """
                    () => Array.from(document.querySelectorAll('div.g, div[data-sokoban-container]'))
                      .map((node) => {
                        const link = node.querySelector('a[href]');
                        const heading = node.querySelector('h3');
                        return {
                          title: (heading?.innerText || '').trim(),
                          url: link?.href || '',
                          excerpt: (node.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 700),
                        };
                      })
                      .filter((row) => row.title && row.url)
                      .slice(0, 20)
                    """
                )
                gathered.extend(results)
                if len(gathered) >= limit:
                    break
            except Exception:
                continue
        if not gathered:
            raise SearchProviderError("blocked")
        return gathered[:limit]
    except SearchProviderError:
        raise
    except Exception as exc:
        raise SearchProviderError("browser_unavailable") from exc
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        try:
            await playwright_ctx.__aexit__(None, None, None)
        except Exception:
            pass


async def _search_public_web(
    queries: List[str],
    limit: int = SEARCH_RESULT_LIMIT,
    relevance_terms: Optional[List[str]] = None,
    address: str = "",
) -> tuple[List[ReviewSource], SearchMeta]:
    relevance_terms = relevance_terms or queries
    gathered: List[ReviewSource] = []
    seen_urls: set[str] = set()
    attempted: List[str] = []
    raw_count = 0
    last_status = "no_relevant_sources"
    diagnostics: List[str] = []
    candidate_limit = max(limit, RSS_CANDIDATE_LIMIT)

    def append_rows(
        rows: List[ReviewSource],
        provider: str,
        allow_search_urls: bool = False,
    ) -> None:
        nonlocal raw_count
        raw_count += len(rows)
        matching = [
            row for row in rows
            if _search_row_matches_terms(row, relevance_terms)
        ]
        _append_unique_search_rows(
            gathered,
            matching,
            seen_urls,
            provider,
            candidate_limit,
            allow_search_urls=allow_search_urls,
        )

    search_mode = os.getenv("REVIEW_SEARCH_MODE", "").strip().lower()
    legacy_browser_flag = os.getenv("USE_PLAYWRIGHT_REVIEW_SEARCH", "").strip().lower()
    if search_mode:
        use_playwright = search_mode in {"auto", "browser", "playwright"}
    elif legacy_browser_flag:
        use_playwright = legacy_browser_flag == "true"
    else:
        use_playwright = True

    if use_playwright and relevance_terms:
        attempted.append("google_maps")
        try:
            maps_rows = await _playwright_google_maps_profile(
                str(relevance_terms[0]), address
            )
            append_rows(maps_rows, "google_maps", allow_search_urls=True)
        except SearchProviderError as exc:
            last_status = exc.status

    attempted.append("ifoodie")
    append_rows(
        _collect_review_sources_via_ifoodie(
            list(relevance_terms), diagnostics=diagnostics, limit=limit
        ),
        "ifoodie",
    )

    attempted.append("bing_rss")
    append_rows(
        _collect_review_sources_via_bing_rss(
            queries, diagnostics=diagnostics, limit=candidate_limit
        ),
        "bing_rss",
    )

    attempted.append("google_news_rss")
    append_rows(
        _collect_review_sources_via_google_news_rss(
            queries, diagnostics=diagnostics, limit=limit
        ),
        "google_news_rss",
    )

    if use_playwright and len(gathered) < MIN_FREE_RESULTS_BEFORE_LEGACY_FALLBACK:
        attempted.append("playwright")
        try:
            playwright_rows = await _playwright_search_queries(queries, limit)
            append_rows(playwright_rows, "playwright")
        except SearchProviderError as exc:
            last_status = exc.status

    if len(gathered) < MIN_FREE_RESULTS_BEFORE_LEGACY_FALLBACK:
        attempted.append("http")
        http_rows = _collect_review_sources_via_html_search(
            queries, set(), diagnostics=diagnostics, limit=limit
        )
        append_rows(http_rows, "http")

    if gathered:
        status = "ok"
        used_providers = list(dict.fromkeys(str(row.get("provider")) for row in gathered))
        provider: Optional[str] = used_providers[0] if len(used_providers) == 1 else "multi"
    else:
        provider = None
        if use_playwright and last_status == "browser_unavailable" and "blocked" not in diagnostics:
            status = "browser_unavailable"
        elif "blocked" in diagnostics:
            status = "blocked"
        elif "network_error" in diagnostics:
            status = "network_error"
        elif use_playwright and last_status in {"blocked", "browser_unavailable"}:
            status = last_status
        else:
            status = "no_relevant_sources"
    return gathered[:candidate_limit], {
        "status": status,
        "provider": provider,
        "attemptedProviders": attempted,
        "rawResultCount": raw_count,
        "relevantSourceCount": 0,
    }


def _collect_review_sources_via_html_search(
    queries: List[str],
    seen_urls: set[str],
    diagnostics: Optional[List[str]] = None,
    limit: int = SEARCH_RESULT_LIMIT,
) -> List[ReviewSource]:
    gathered: List[ReviewSource] = []
    for query in queries:
        gathered.extend(
            _collect_review_sources_via_jina_google(
                query, seen_urls, limit - len(gathered), diagnostics=diagnostics
            )
        )
        if len(gathered) >= limit:
            return gathered[:limit]

        for search_url, parser_factory in [
            (
                f"https://duckduckgo.com/html/?q={quote_plus(query)}",
                DuckDuckGoHTMLParser,
            ),
            (
                f"https://www.bing.com/search?q={quote_plus(query)}&setlang=zh-TW",
                BingHTMLParser,
            ),
        ]:
            try:
                html = _fetch_search_html(search_url)
                parser = parser_factory()
                parser.feed(html)
                for row in parser.results:
                    url = _clean_search_url(row.get("url", ""))
                    title = unescape(row.get("title", "")).strip()
                    excerpt = unescape(row.get("excerpt", "")).strip()
                    if not url or url in seen_urls or _is_youtube_url(url):
                        continue
                    if not title or len(f"{title} {excerpt}") < 12:
                        continue
                    seen_urls.add(url)
                    gathered.append(
                        {
                            "title": title[:120],
                            "url": url,
                            "excerpt": re.sub(r"\s+", " ", excerpt or title)[:500],
                            "sourceType": _classify_source(url, title),
                        }
                    )
                    if len(gathered) >= limit:
                        return gathered
            except Exception as exc:
                _record_http_search_error(diagnostics, exc)
                continue
    return gathered[:limit]


def _collect_review_sources_via_jina_google(
    query: str,
    seen_urls: set[str],
    limit: int,
    diagnostics: Optional[List[str]] = None,
) -> List[ReviewSource]:
    if limit <= 0:
        return []
    url = f"https://r.jina.ai/http://www.google.com/search?q={quote_plus(query)}"
    try:
        markdown = _fetch_search_html(url)
    except Exception as exc:
        _record_http_search_error(diagnostics, exc)
        return []

    rows: List[ReviewSource] = []
    lines = markdown.splitlines()
    for idx, line in enumerate(lines):
        match = re.search(r"\[([^\]]{2,160})\]\((https?://[^)]+)\)", line)
        if not match:
            continue
        title = _strip_markdown(match.group(1))
        target_url = _clean_search_url(match.group(2))
        if not target_url or target_url in seen_urls or _is_youtube_url(target_url):
            continue
        title_lower = title.lower()
        if (
            _is_search_noise_url(target_url)
            or title_lower.startswith(("image ", "translate this page"))
            or title_lower in {"read more", "website", "menu"}
        ):
            continue

        excerpt_parts: List[str] = []
        for follow in lines[idx + 1 : idx + 5]:
            clean = _strip_markdown(follow).strip()
            if not clean or clean.startswith(("### ", "[", "!", "http")):
                continue
            excerpt_parts.append(clean)
        excerpt = re.sub(r"\s+", " ", " ".join(excerpt_parts))[:500]
        if len(f"{title} {excerpt}") < 12:
            continue

        seen_urls.add(target_url)
        rows.append(
            {
                "title": title[:120],
                "url": target_url,
                "excerpt": excerpt or title,
                "sourceType": _classify_source(target_url, title),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _strip_markdown(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^[#*\s·-]+", "", text)
    return unescape(text).strip()


def _is_search_noise_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not host:
        return True
    if host == "news.google.com" and parsed.path.startswith("/rss/articles/"):
        return False
    noise_hosts = {
        "www.google.com",
        "google.com",
        "support.google.com",
        "accounts.google.com",
        "webcache.googleusercontent.com",
    }
    return host in noise_hosts or host.endswith(".google.com")


def _fetch_search_html(url: str) -> str:
    req = request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
            ),
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
        },
    )
    with request.urlopen(req, timeout=15) as resp:
        raw = resp.read(MAX_SEARCH_RESPONSE_BYTES)
        charset = resp.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def _clean_search_url(url: str) -> str:
    if not url:
        return ""
    url = unescape(url)
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        uddg = parse_qs(parsed.query).get("uddg", [""])[0]
        if uddg:
            return unquote(uddg)
    if "bing.com" in parsed.netloc and parsed.path.startswith("/ck/a"):
        target = parse_qs(parsed.query).get("u", [""])[0]
        if target:
            if target.startswith("a1"):
                target = target[2:]
            try:
                import base64

                padded = target + "=" * (-len(target) % 4)
                return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
            except Exception:
                return target
    if parsed.scheme in {"http", "https"}:
        return url
    return ""


class DuckDuckGoHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: List[ReviewSource] = []
        self._current: Optional[ReviewSource] = None
        self._capture: Optional[str] = None

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        attr = dict(attrs)
        classes = attr.get("class", "") or ""
        if tag == "a" and "result__a" in classes:
            self._current = {"title": "", "url": attr.get("href", "") or "", "excerpt": "", "sourceType": "web"}
            self._capture = "title"
        elif self._current is not None and tag in {"a", "div"} and "result__snippet" in classes:
            self._capture = "excerpt"

    def handle_data(self, data: str) -> None:
        if self._current is not None and self._capture:
            self._current[self._capture] = (self._current.get(self._capture, "") + data).strip()

    def handle_endtag(self, tag: str) -> None:
        if self._current is not None and tag == "a" and self._capture == "title":
            self._capture = None
        elif self._current is not None and tag in {"a", "div"} and self._capture == "excerpt":
            self._capture = None
            if self._current.get("title"):
                self.results.append(self._current)
                self._current = None


class BingHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: List[ReviewSource] = []
        self._current: Optional[ReviewSource] = None
        self._in_result = False
        self._capture: Optional[str] = None

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        attr = dict(attrs)
        classes = attr.get("class", "") or ""
        if tag == "li" and "b_algo" in classes:
            self._in_result = True
            self._current = {"title": "", "url": "", "excerpt": "", "sourceType": "web"}
        elif self._in_result and tag == "a" and self._current is not None and not self._current.get("url"):
            self._current["url"] = attr.get("href", "") or ""
            self._capture = "title"
        elif self._in_result and tag == "p" and self._current is not None:
            self._capture = "excerpt"

    def handle_data(self, data: str) -> None:
        if self._current is not None and self._capture:
            self._current[self._capture] = (self._current.get(self._capture, "") + data).strip()

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture == "title":
            self._capture = None
        elif tag == "p" and self._capture == "excerpt":
            self._capture = None
        elif tag == "li" and self._in_result and self._current is not None:
            if self._current.get("title") and self._current.get("url"):
                self.results.append(self._current)
            self._current = None
            self._in_result = False


def _is_youtube_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host in YOUTUBE_HOSTS or host.endswith(".youtube.com")


def _classify_source(url: str, title: str) -> str:
    host = urlparse(url).netloc.lower()
    haystack = f"{host} {title}".lower()
    if host == "news.google.com":
        return "news"
    if "google" in host:
        return "google"
    if any(site in haystack for site in [
        "blog", "pixnet", "medium", "wordpress", "痞客邦",
        "popdaily", "walkerland",
    ]):
        return "blog"
    if any(site in haystack for site in ["dcard", "ptt", "forum", "mobile01"]):
        return "forum"
    return "web"


def _extract_json_object(raw: str) -> Optional[Dict[str, Any]]:
    match = re.search(r"\{.*\}", raw, flags=re.S)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _string_list(value: Any, fallback: List[str], limit: int) -> List[str]:
    if not isinstance(value, list):
        return fallback
    cleaned = [str(item).strip()[:80] for item in value if str(item).strip()]
    return cleaned[:limit] or fallback


# Review intelligence schema v3 adds source-provider diagnostics to the
# identity, source-quality and evidence pipeline.
SCHEMA_VERSION = 3
ASPECT_DEFINITIONS = {
    "taste": {
        "label": "口味",
        "weight": 0.30,
        "keywords": ["好吃", "美味", "味道", "口味", "新鮮", "油膩", "難吃", "餐點", "料理"],
    },
    "value": {
        "label": "價格",
        "weight": 0.20,
        "keywords": [
            "價格", "價位", "便宜", "划算", "昂貴", "太貴", "CP值", "消費",
            "套餐", "人均", "低消", "元",
        ],
    },
    "service": {
        "label": "服務",
        "weight": 0.15,
        "keywords": ["服務", "店員", "態度", "親切", "招呼", "出餐"],
    },
    "environment": {
        "label": "環境",
        "weight": 0.15,
        "keywords": ["環境", "座位", "乾淨", "整潔", "吵", "舒適", "停車", "裝潢"],
    },
    "portion": {
        "label": "份量",
        "weight": 0.10,
        "keywords": [
            "份量", "分量", "吃飽", "很少", "很多", "大份", "小份", "吃到飽", "自助吧",
        ],
    },
    "waitTime": {
        "label": "等候時間",
        "weight": 0.10,
        "keywords": [
            "等很久", "等待", "排隊", "候位", "出餐快", "出餐慢", "訂位", "尖峰",
            "客滿", "人多", "限時", "用餐時間", "預約",
        ],
    },
}
POSITIVE_TERMS = [
    "好吃", "美味", "新鮮", "划算", "便宜", "親切", "乾淨", "舒適", "推薦",
    "值得", "快速", "充足", "很大", "方便", "回訪",
]
NEGATIVE_TERMS = [
    "難吃", "普通", "油膩", "不新鮮", "太貴", "昂貴", "態度差", "髒", "吵",
    "等很久", "出餐慢", "很少", "失望", "踩雷", "不值",
]
REVIEW_PLATFORM_HOSTS = {
    "google.com", "www.google.com", "maps.google.com", "ubereats.com", "www.ubereats.com",
    "foodpanda.com.tw", "www.foodpanda.com.tw", "inline.app", "www.inline.app",
    "openrice.com", "tw.openrice.com", "ifoodie.tw", "www.ifoodie.tw",
    "tripadvisor.com", "www.tripadvisor.com", "tripadvisor.com.tw", "www.tripadvisor.com.tw",
    "fooday.app", "www.fooday.app", "iwans.tw", "www.iwans.tw",
}
AGGREGATOR_MARKERS = [
    "footinder", "timetables.tw", "gotoformosa", "restaurantguru",
    "wanderlog",
]
TRACKING_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "ref_src", "source",
}


def _empty_review_report(
    restaurant_name: str,
    message: str = "尚未更新評價",
    identity: Optional[Dict[str, Any]] = None,
) -> ReviewReport:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "success": False,
        "message": message,
        "restaurantName": restaurant_name,
        "restaurantIdentity": identity,
        "needsIdentity": identity is None,
        "needsRefresh": True,
        "updatedAt": None,
        "recommendationScore": 0,
        "confidenceScore": 0,
        "overallScore": 0,
        "scoreBasis": "aspects",
        "platformRating": None,
        "sentiment": "unknown",
        "summary": "",
        "pros": [],
        "cons": [],
        "prosEvidence": [],
        "consEvidence": [],
        "recommendedFor": [],
        "aspects": _empty_aspects(),
        "riskLevel": "unknown",
        "riskReasons": ["資料不足，尚無法判斷評價可信度"],
        "riskSignals": [],
        "evidence": [],
        "sources": [],
        "searchMeta": _empty_search_meta(),
    }


def _empty_aspects() -> Dict[str, Dict[str, Any]]:
    return {
        key: {
            "label": spec["label"],
            "score": None,
            "confidence": 0,
            "mentionCount": 0,
            "evidenceIds": [],
            "status": "insufficient",
        }
        for key, spec in ASPECT_DEFINITIONS.items()
    }


def _identity_id(name: str, address: str) -> str:
    raw = f"{name.strip().lower()}|{address.strip().lower()}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


def _normalize_identity(value: Any, fallback_name: str) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    name = str(value.get("officialName") or value.get("name") or fallback_name).strip()
    address = str(value.get("address") or "").strip()
    if not name:
        return None
    maps_url = str(value.get("mapsUrl") or "").strip()
    if not maps_url:
        maps_url = (
            "https://www.google.com/maps/search/?api=1&query="
            + quote_plus(" ".join(part for part in [name, address] if part))
        )
    return {
        "identityId": str(value.get("identityId") or _identity_id(name, address)),
        "officialName": name[:120],
        "queryName": str(value.get("queryName") or fallback_name)[:120],
        "address": address[:180],
        "mapsUrl": maps_url,
        "website": str(value.get("website") or ""),
        "evidence": str(value.get("evidence") or "")[:500],
        "confidence": max(0, min(100, int(value.get("confidence") or 0))),
        "confirmed": bool(value.get("confirmed", True)),
    }


def _extract_address(text: str) -> str:
    clean = re.sub(r"\s+", " ", text)
    patterns = [
        r"(?:\d{3}\s*)?(?:台|臺)[北中南東][市縣][^,，。|]{1,36}?(?:路|街|大道|巷)(?:[一二三四五六七八九十\d]+段)?\s*\d+(?:之\d+)?號",
        r"(?:\d{3}\s*)?(?:台|臺)灣[^,，。|]{2,40}?(?:路|街|大道|巷)(?:[一二三四五六七八九十\d]+段)?\s*\d+(?:之\d+)?號",
    ]
    for pattern in patterns:
        match = re.search(pattern, clean)
        if match:
            return match.group(0).strip()
    return ""


def _candidate_name(title: str, restaurant_name: str) -> str:
    cleaned = re.sub(r"\s*[-|｜].*$", "", title).strip()
    cleaned = re.sub(r"(地址|電話|菜單|評價|食記|訂位|營業時間).*$", "", cleaned).strip(" -|｜")
    if (
        len(cleaned) < 2
        or len(cleaned) > max(30, len(restaurant_name) * 2)
        or any(marker in cleaned for marker in ["【", "】", "？", "?", "怎樣", "推薦懶人包"])
    ):
        return restaurant_name
    return cleaned


def identify_restaurant_candidates(restaurant_name: str) -> List[Dict[str, Any]]:
    name = restaurant_name.replace("_", " ").strip()
    if not name:
        return []
    queries = [f"{name} 地址", f"{name} Google Maps 分店"]
    sources, _ = asyncio.run(
        _search_public_web(queries, relevance_terms=[name])
    )
    candidates: List[Dict[str, Any]] = []
    seen: set[str] = set()
    normalized_target = re.sub(r"\W+", "", name).lower()

    for source in sources:
        text = f"{source.get('title', '')} {source.get('excerpt', '')}"
        candidate_name = _candidate_name(str(source.get("title", "")), name)
        address = _extract_address(text)
        normalized_candidate = re.sub(r"\W+", "", candidate_name).lower()
        similarity = SequenceMatcher(None, normalized_target, normalized_candidate).ratio()
        if name not in text and similarity < 0.35:
            continue
        compact_address = re.sub(r"\s+", "", address)
        key = f"{normalized_candidate}|{compact_address}"
        if key in seen:
            continue
        seen.add(key)
        query = " ".join(part for part in [candidate_name, address] if part)
        candidates.append(
            {
                "identityId": _identity_id(candidate_name, address),
                "officialName": candidate_name,
                "address": address,
                "mapsUrl": f"https://www.google.com/maps/search/?api=1&query={quote_plus(query)}",
                "website": str(source.get("url") or ""),
                "confidence": min(95, round(45 + similarity * 35 + (15 if address else 0))),
                "evidence": str(source.get("excerpt") or "")[:220],
            }
        )
        if len(candidates) >= 5:
            break

    candidates.sort(
        key=lambda item: (
            bool(item.get("address")),
            int(item.get("confidence") or 0),
        ),
        reverse=True,
    )
    if not candidates:
        candidates.append(
            {
                "identityId": _identity_id(name, ""),
                "officialName": name,
                "address": "",
                "mapsUrl": f"https://www.google.com/maps/search/?api=1&query={quote_plus(name)}",
                "website": "",
                "confidence": 20,
                "evidence": "搜尋不到明確地址，請先由 Google Maps 連結確認店家。",
            }
        )
    return candidates


def _canonicalize_url(url: str) -> str:
    parsed = urlparse(_clean_search_url(url))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    query = [
        (key, value)
        for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
        if key.lower() not in TRACKING_QUERY_KEYS
        for value in values
    ]
    path = re.sub(r"/+$", "", parsed.path) or "/"
    return urlunparse(
        (parsed.scheme.lower(), parsed.netloc.lower(), path, "", urlencode(query), "")
    )


def _source_type_and_quality(url: str, title: str) -> tuple[str, float]:
    host = urlparse(url).netloc.lower()
    haystack = f"{host} {title}".lower()
    if host == "news.google.com":
        return "news", 0.72
    if any(host == item or host.endswith("." + item) for item in REVIEW_PLATFORM_HOSTS):
        return "review_platform", 0.90
    if any(site in haystack for site in ["dcard", "ptt", "mobile01", "forum"]):
        return "forum", 0.80
    if any(marker in haystack for marker in AGGREGATOR_MARKERS):
        return "aggregator", 0.45
    if any(site in haystack for site in [
        "blog", "pixnet", "wordpress", "medium", "痞客邦",
        "popdaily", "walkerland",
    ]):
        return "blog", 0.68
    return "web", 0.62


def _parse_date(value: str) -> Optional[str]:
    if not value:
        return None
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", value)
    if not match:
        return None
    try:
        return datetime(
            int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=timezone.utc
        ).isoformat()
    except ValueError:
        return None


def _extract_page(url: str) -> Dict[str, Any]:
    req = request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
            ),
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
        },
    )
    with request.urlopen(req, timeout=12) as resp:
        raw = resp.read(1_500_000)
        charset = resp.headers.get_content_charset() or "utf-8"
        content_type = resp.headers.get("Content-Type", "")
    if "html" not in content_type.lower():
        raise ValueError("來源不是 HTML")
    html = raw.decode(charset, errors="replace")

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
    canonical_url = canonical.get("href", "") if canonical else ""
    date_value = ""
    for key, attr in [
        ("article:published_time", "property"),
        ("datePublished", "itemprop"),
        ("date", "name"),
        ("pubdate", "name"),
    ]:
        node = soup.find("meta", attrs={attr: key})
        if node and node.get("content"):
            date_value = str(node.get("content"))
            break
    for node in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
        node.decompose()
    container = soup.find("article") or soup.find("main") or soup.body
    text = " ".join(container.stripped_strings) if container else ""
    text = re.sub(r"\s+", " ", text)[:12000]
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    return {
        "title": title[:160],
        "content": text,
        "publishedAt": _parse_date(date_value) or _parse_date(text[:1200]),
        "canonicalUrl": canonical_url,
    }


def _enrich_source(source: ReviewSource, identity: Dict[str, Any]) -> ReviewSource:
    now = datetime.now(timezone.utc).isoformat()
    url = _canonicalize_url(str(source.get("url") or ""))
    title = str(source.get("title") or "")[:160]
    excerpt = re.sub(r"\s+", " ", str(source.get("excerpt") or ""))[:700]
    source_type, quality = _source_type_and_quality(url, title)
    result: ReviewSource = {
        "sourceId": "",
        "title": title,
        "url": url,
        "canonicalUrl": url,
        "excerpt": excerpt,
        "content": excerpt,
        "sourceType": source_type,
        "publishedAt": source.get("publishedAt") or _parse_date(f"{title} {excerpt}"),
        "retrievedAt": now,
        "sourceQuality": quality,
        "sponsored": False,
        "duplicateOf": None,
        "fetchStatus": "snippet",
        "provider": str(source.get("provider") or "unknown"),
        "rank": max(0, int(source.get("rank") or 0)),
        "platformRating": source.get("platformRating"),
    }
    if not (
        result["provider"] == "google_news_rss"
        and urlparse(url).netloc.lower() == "news.google.com"
    ):
        try:
            page = _extract_page(url)
            result["title"] = page.get("title") or title
            result["content"] = page.get("content") or excerpt
            result["publishedAt"] = page.get("publishedAt") or result["publishedAt"]
            canonical = _canonicalize_url(str(page.get("canonicalUrl") or ""))
            if canonical:
                result["canonicalUrl"] = canonical
                result["url"] = canonical
            result["fetchStatus"] = "full"
        except Exception:
            pass

    combined = f"{result['title']} {result['excerpt']} {result['content']}"
    result["sponsored"] = any(keyword in combined for keyword in SPONSORED_KEYWORDS)
    if result["sponsored"]:
        result["sourceQuality"] = round(float(result["sourceQuality"]) * 0.45, 3)
    host = urlparse(str(result["url"])).netloc
    source_key = f"{result['canonicalUrl']}|{host}|{result['title']}"
    result["sourceId"] = "SRC-" + hashlib.sha1(source_key.encode("utf-8")).hexdigest()[:10]
    return result


def _is_review_candidate(source: ReviewSource) -> bool:
    url = str(source.get("url") or "")
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    title = str(source.get("title") or "").lower()
    if host in SOCIAL_MEDIA_HOSTS or any(host.endswith("." + item) for item in SOCIAL_MEDIA_HOSTS):
        return False
    if host in {"inline.app", "www.inline.app"} and re.search(r"/(?:booking|order)/", path):
        return False
    if any(marker in title for marker in ["登入", "login", "sign in", "徵才", "職缺"]):
        return False
    return True


def _branch_aliases(identity: Dict[str, Any]) -> List[str]:
    aliases: List[str] = []
    names = _restaurant_search_names(identity)
    for name in names:
        parts = [part for part in re.split(r"[\s_－—|｜]+", name) if part]
        for index, part in enumerate(parts):
            if index == 0 and len(parts) > 1:
                continue
            normalized = re.sub(r"\W+", "", part).lower()
            variants = [
                normalized,
                re.sub(r"店$", "", normalized),
                re.sub(r"^(?:台|臺)[北中南東]", "", normalized),
                re.sub(r"店$", "", re.sub(r"^(?:台|臺)[北中南東]", "", normalized)),
            ]
            for value in variants:
                if len(value) >= 2 and value not in aliases:
                    aliases.append(value)

    address = str(identity.get("address") or "")
    for match in re.findall(r"((?:台|臺)[北中南東][市縣]|[^\s]{1,6}[區鄉鎮市])", address):
        value = re.sub(r"\W+", "", match).lower()
        if len(value) >= 2 and value not in aliases:
            aliases.append(value)
    return aliases


def _has_conflicting_branch(text: str, branch_aliases: List[str]) -> bool:
    compact = re.sub(r"\W+", "", text).lower()
    if not branch_aliases or any(alias in compact for alias in branch_aliases):
        return False
    branch_markers = re.findall(
        r"((?:(?:台|臺)[北中南東]|高雄|桃園|新竹|基隆|嘉義|彰化|員林|逢甲|東海|中科|公益|黎明|洲際|秀泰|台鋁)[^，。|｜]{0,6}店)",
        text,
    )
    return bool(branch_markers)


def _source_relevant(source: ReviewSource, identity: Dict[str, Any]) -> bool:
    title = str(source.get("title") or "")
    content = f"{title} {source.get('excerpt', '')} {source.get('content', '')[:1000]}"
    lowered = content.lower()
    if any(
        marker in lowered
        for marker in ["人力銀行", "職缺", "徵才", "服務員(兼職)", "104.com", "1111.com"]
    ):
        return False
    names = _restaurant_search_names(identity)
    tokens = []
    for name in names:
        tokens.extend(
            token
            for token in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", name)
            if token not in {"台中", "餐廳", "分店"}
        )
    if not tokens:
        return True
    if any(token.lower() in lowered for token in tokens):
        return not _has_conflicting_branch(
            f"{title} {source.get('excerpt', '')}", _branch_aliases(identity)
        )
    compact_title = re.sub(r"\W+", "", title).lower()
    compact_name = re.sub(r"\W+", "", str(identity.get("officialName") or "")).lower()
    return bool(compact_title and compact_name) and SequenceMatcher(
        None, compact_title, compact_name
    ).ratio() >= 0.32


def _content_fingerprint(source: ReviewSource) -> str:
    text = str(source.get("content") or source.get("excerpt") or "")
    return re.sub(r"[\W_]+", "", text.lower())[:1000]


def _mark_duplicates(sources: List[ReviewSource]) -> None:
    canonical_seen: Dict[str, str] = {}
    unique: List[ReviewSource] = []
    for source in sources:
        canonical = str(source.get("canonicalUrl") or source.get("url") or "")
        if canonical in canonical_seen:
            source["duplicateOf"] = canonical_seen[canonical]
            source["sourceQuality"] = 0.0
            continue
        fingerprint = _content_fingerprint(source)
        duplicate_id = None
        if len(fingerprint) >= 80:
            for previous in unique:
                previous_fp = _content_fingerprint(previous)
                if previous_fp and SequenceMatcher(None, fingerprint, previous_fp).ratio() >= 0.88:
                    duplicate_id = str(previous.get("sourceId"))
                    break
        if duplicate_id:
            source["duplicateOf"] = duplicate_id
            source["sourceQuality"] = 0.0
        else:
            canonical_seen[canonical] = str(source.get("sourceId"))
            unique.append(source)


def _direct_source_rows(
    identity: Dict[str, Any], source_urls: Optional[List[str]] = None
) -> List[ReviewSource]:
    official_name = str(identity.get("officialName") or identity.get("queryName") or "餐廳")
    candidates: List[tuple[str, str]] = []
    compact_names = {
        re.sub(r"\W+", "", str(value or "")).lower()
        for value in [identity.get("officialName"), identity.get("queryName")]
        if value
    }
    for key, urls in KNOWN_REVIEW_URLS.items():
        if re.sub(r"\W+", "", key).lower() in compact_names:
            candidates.extend((url, "known_url") for url in urls)
    website = _canonicalize_url(str(identity.get("website") or ""))
    if website:
        source_type, _ = _source_type_and_quality(website, official_name)
        if source_type in {"review_platform", "forum", "blog", "aggregator", "news"}:
            candidates.append((website, "known_url"))
    identity_urls = identity.get("sourceUrls") or []
    if isinstance(identity_urls, str):
        identity_urls = [identity_urls]
    for value in list(identity_urls) + list(source_urls or []):
        url = _canonicalize_url(str(value or ""))
        if url:
            candidates.append((url, "user_url"))

    rows: List[ReviewSource] = []
    seen: set[str] = set()
    for url, provider in candidates:
        if url in seen or _is_youtube_url(url) or _is_search_noise_url(url):
            continue
        seen.add(url)
        rows.append(
            {
                "title": f"{official_name} 公開評價來源",
                "url": url,
                "excerpt": "使用已知餐廳頁面或使用者提供的公開文章網址。",
                "sourceType": _classify_source(url, official_name),
                "provider": provider,
                "rank": len(rows) + 1,
            }
        )
        if len(rows) >= 8:
            break
    return rows


def _collect_enriched_sources(
    identity: Dict[str, Any],
    source_urls: Optional[List[str]] = None,
) -> tuple[List[ReviewSource], SearchMeta]:
    official_name = str(identity.get("officialName") or "").strip()
    query_name = str(identity.get("queryName") or "").strip()
    search_names = _restaurant_search_names(identity)
    search_name = (search_names[0] if search_names else official_name or query_name).strip()
    address = str(identity.get("address") or "").strip()
    location_hint = " ".join(address.split()[:2]) if address else ""
    queries: List[str] = []
    for name in search_names[:3] or [search_name]:
        exact_name = f'"{name}"'
        queries.extend(
            [
                f"{exact_name} 評價 食記",
                f"{exact_name} 部落格 Dcard PTT",
            ]
        )
    primary_exact_name = f'"{search_name}"'
    if location_hint:
        queries.append(f"{primary_exact_name} {location_hint} 評論")
    queries.extend(
        [
            f"{primary_exact_name} site:dcard.tw/f/food OR site:ptt.cc/bbs/Food",
            (
                f"{primary_exact_name} site:ifoodie.tw OR site:pixnet.net "
                "OR site:popdaily.com.tw OR site:walkerland.com.tw"
            ),
        ]
    )
    raw_sources, search_meta = asyncio.run(
        _search_public_web(
            queries,
            relevance_terms=search_names,
            address=address,
        )
    )
    direct_sources = _direct_source_rows(identity, source_urls)
    if direct_sources:
        raw_sources.extend(direct_sources)
        search_meta["rawResultCount"] = int(search_meta.get("rawResultCount") or 0) + len(direct_sources)
        attempted = list(search_meta.get("attemptedProviders") or [])
        for provider in [str(row.get("provider")) for row in direct_sources]:
            if provider not in attempted:
                attempted.append(provider)
        search_meta["attemptedProviders"] = attempted

    unique_raw_sources: List[ReviewSource] = []
    seen_raw_urls: set[str] = set()
    for source in raw_sources:
        canonical = _canonicalize_url(str(source.get("url") or ""))
        if not canonical or canonical in seen_raw_urls:
            continue
        if not _is_review_candidate(source) or not _source_relevant(source, identity):
            continue
        seen_raw_urls.add(canonical)
        unique_raw_sources.append(source)
    raw_sources = unique_raw_sources
    enriched: List[ReviewSource] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_enrich_source, source, identity) for source in raw_sources]
        for future in as_completed(futures):
            try:
                source = future.result()
                if (
                    source.get("provider") in {"known_url", "user_url"}
                    and source.get("fetchStatus") != "full"
                ):
                    continue
                if source.get("url") and _source_relevant(source, identity):
                    enriched.append(source)
            except Exception:
                continue
    enriched.sort(key=lambda item: float(item.get("sourceQuality") or 0), reverse=True)
    _mark_duplicates(enriched)
    enriched = enriched[:SEARCH_RESULT_LIMIT]
    search_meta["relevantSourceCount"] = len(
        [source for source in enriched if not source.get("duplicateOf")]
    )
    if enriched:
        used_providers = list(
            dict.fromkeys(str(source.get("provider") or "") for source in enriched)
        )
        search_meta["status"] = "ok"
        search_meta["provider"] = (
            used_providers[0] if len(used_providers) == 1 else "multi"
        )
    elif search_meta.get("status") == "ok":
        search_meta["status"] = "no_relevant_sources"
    return enriched, search_meta


def _recency_weight(published_at: Any) -> float:
    if not published_at:
        return 0.60
    try:
        date = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        age_days = max(0, (datetime.now(timezone.utc) - date).days)
    except Exception:
        return 0.60
    if age_days <= 183:
        return 1.0
    if age_days <= 548:
        return 0.85
    if age_days <= 1095:
        return 0.65
    return 0.40


def _split_sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[。！？!?])|\s*[|｜]\s*", re.sub(r"\s+", " ", text))
    return [part.strip() for part in parts if 8 <= len(part.strip()) <= 220]


def _polarity(sentence: str) -> str:
    positive = sum(term in sentence for term in POSITIVE_TERMS)
    negative = sum(term in sentence for term in NEGATIVE_TERMS)
    if positive > negative:
        return "positive"
    if negative > positive:
        return "negative"
    return "neutral"


def _extract_rule_evidence(sources: List[ReviewSource]) -> List[Dict[str, Any]]:
    evidence: List[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for source in sources:
        if source.get("duplicateOf"):
            continue
        text = str(source.get("content") or source.get("excerpt") or "")
        for sentence in _split_sentences(text):
            for aspect, spec in ASPECT_DEFINITIONS.items():
                if not any(keyword.lower() in sentence.lower() for keyword in spec["keywords"]):
                    continue
                key = (str(source.get("sourceId")), sentence[:100])
                if key in seen:
                    continue
                seen.add(key)
                evidence.append(
                    {
                        "evidenceId": f"EV-{len(evidence) + 1:03d}",
                        "sourceId": source.get("sourceId"),
                        "aspect": aspect,
                        "polarity": _polarity(sentence),
                        "text": sentence[:220],
                        "weight": round(
                            float(source.get("sourceQuality") or 0)
                            * _recency_weight(source.get("publishedAt")),
                            3,
                        ),
                    }
                )
                if len(evidence) >= 60:
                    return evidence
    return evidence


def _calculate_aspects(evidence: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    result = _empty_aspects()
    polarity_scores = {"positive": 82, "neutral": 55, "negative": 28}
    for aspect in ASPECT_DEFINITIONS:
        rows = [item for item in evidence if item.get("aspect") == aspect]
        source_ids = {str(item.get("sourceId")) for item in rows if item.get("sourceId")}
        result[aspect]["mentionCount"] = len(rows)
        result[aspect]["evidenceIds"] = [str(item["evidenceId"]) for item in rows[:8]]
        if not source_ids:
            continue
        total_weight = sum(max(0.05, float(item.get("weight") or 0)) for item in rows)
        weighted_score = sum(
            polarity_scores.get(str(item.get("polarity")), 55)
            * max(0.05, float(item.get("weight") or 0))
            for item in rows
        ) / total_weight
        avg_quality = total_weight / max(1, len(rows))
        result[aspect]["score"] = round(weighted_score)
        result[aspect]["confidence"] = min(
            100, round(8 + len(source_ids) * 16 + min(30, avg_quality * 35))
        )
        result[aspect]["status"] = "supported" if len(source_ids) >= 2 else "estimated"
    return result


def _recommendation_score(aspects: Dict[str, Dict[str, Any]]) -> int:
    available = [
        (key, value)
        for key, value in aspects.items()
        if isinstance(value.get("score"), (int, float))
    ]
    if not available:
        return 0
    weight_sum = sum(float(ASPECT_DEFINITIONS[key]["weight"]) for key, _ in available)
    return round(
        sum(
            float(value["score"]) * float(ASPECT_DEFINITIONS[key]["weight"])
            for key, value in available
        )
        / weight_sum
    )


def _platform_rating(sources: List[ReviewSource]) -> Optional[Dict[str, Any]]:
    rows = []
    for source in sources:
        if source.get("duplicateOf"):
            continue
        explicit = source.get("platformRating")
        if isinstance(explicit, dict) and isinstance(explicit.get("average"), (int, float)):
            rating = float(explicit["average"])
            if 1 <= rating <= 5:
                rows.append(
                    {
                        "rating": rating,
                        "reviewCount": (
                            int(explicit["reviewCount"])
                            if isinstance(explicit.get("reviewCount"), (int, float))
                            else None
                        ),
                        "sourceId": source.get("sourceId"),
                        "platform": str(explicit.get("platform") or ""),
                    }
                )
                continue
        text = f"{source.get('title', '')} {source.get('excerpt', '')} {source.get('content', '')}"
        match = re.search(r"([1-5](?:\.\d{1,2})?)\s*/\s*5", text)
        if not match:
            match = re.search(r"評分(?:為)?\s*([1-5](?:\.\d{1,2})?)\s*星", text)
        if not match:
            continue
        rating = float(match.group(1))
        count_match = re.search(r"([\d,]+)\+?\s*(?:則評論|票|votes|reviews)", text, re.I)
        rows.append(
            {
                "rating": rating,
                "reviewCount": int(count_match.group(1).replace(",", "")) if count_match else None,
                "sourceId": source.get("sourceId"),
                "platform": "",
            }
        )
    if not rows:
        return None
    google_rows = [row for row in rows if row.get("platform") == "Google Maps"]
    selected_rows = google_rows or rows
    return {
        "average": round(
            sum(row["rating"] for row in selected_rows) / len(selected_rows), 2
        ),
        "ratingCount": len(selected_rows),
        "reviewCount": max(
            (row["reviewCount"] or 0 for row in selected_rows), default=0
        ) or None,
        "sourceIds": [
            str(row["sourceId"]) for row in selected_rows if row.get("sourceId")
        ],
        "platform": "Google Maps" if google_rows else "公開評論平台",
    }


def _confidence_score(
    identity: Optional[Dict[str, Any]],
    sources: List[ReviewSource],
    aspects: Dict[str, Dict[str, Any]],
) -> int:
    unique = [source for source in sources if not source.get("duplicateOf")]
    domains = {urlparse(str(source.get("url") or "")).netloc for source in unique}
    quality = (
        sum(float(source.get("sourceQuality") or 0) for source in unique) / len(unique)
        if unique else 0
    )
    dated_ratio = (
        sum(bool(source.get("publishedAt")) for source in unique) / len(unique)
        if unique else 0
    )
    scored_aspects = sum(value.get("score") is not None for value in aspects.values())
    score = (
        (20 if identity and identity.get("confirmed") else 0)
        + min(20, len(unique) * 4)
        + min(20, len(domains) * 4)
        + quality * 20
        + dated_ratio * 10
        + scored_aspects / len(ASPECT_DEFINITIONS) * 10
    )
    return min(100, round(score))


def _risk_evidence(
    sources: List[ReviewSource],
    base_evidence: List[Dict[str, Any]],
) -> tuple[str, List[str], List[Dict[str, Any]], List[Dict[str, Any]]]:
    evidence = list(base_evidence)
    signals: List[Dict[str, Any]] = []

    def add_signal(label: str, level_points: int, source: ReviewSource, text: str) -> None:
        evidence_id = f"EV-{len(evidence) + 1:03d}"
        evidence.append(
            {
                "evidenceId": evidence_id,
                "sourceId": source.get("sourceId"),
                "aspect": "credibility",
                "polarity": "risk",
                "text": text[:220],
                "weight": float(source.get("sourceQuality") or 0),
            }
        )
        signals.append({"label": label, "points": level_points, "evidenceIds": [evidence_id]})

    for source in sources:
        text = f"{source.get('excerpt', '')} {source.get('content', '')}"
        hits = [keyword for keyword in INCENTIVE_KEYWORDS if keyword in text]
        if hits:
            add_signal(
                f"找到誘導評論線索：{', '.join(sorted(set(hits)))}",
                4,
                source,
                next((sentence for sentence in _split_sentences(text) if any(hit in sentence for hit in hits)), text),
            )
        if source.get("sponsored"):
            add_signal("來源標示為業配、合作或邀約，已降低分析權重", 0, source, text)

        excerpt = re.sub(r"\s+", "", str(source.get("excerpt") or ""))
        concrete_mentions = re.findall(
            r"(牛肉|雞|豬|魚|麵|飯|湯|鍋|咖哩|燒肉|甜點|飲料|排|堡|服務|價格|環境)",
            excerpt,
        )
        if (
            source.get("sourceType") in {"review_platform", "forum"}
            and 0 < len(excerpt) <= 28
            and any(keyword in excerpt for keyword in SHORT_PRAISE)
            and not concrete_mentions
        ):
            add_signal("短句正面評價缺少具體餐點或體驗細節", 1, source, excerpt)

    duplicate_sources = [source for source in sources if source.get("duplicateOf")]
    if duplicate_sources:
        source = duplicate_sources[0]
        add_signal("部分內容高度相似或可能為重複轉載", min(3, len(duplicate_sources)), source, str(source.get("excerpt") or source.get("title")))

    review_like = [
        source for source in sources
        if source.get("sourceType") in {"review_platform", "forum"} and not source.get("duplicateOf")
    ]
    points = sum(int(signal["points"]) for signal in signals)
    if points >= 6:
        level = "high"
    elif points >= 3:
        level = "medium"
    elif len(review_like) >= 3:
        level = "low"
    else:
        level = "unknown"
    reasons = [str(signal["label"]) for signal in signals]
    if level == "unknown" and not reasons:
        reasons = ["缺少足夠的逐則評論，暫時無法判斷灌水風險"]
    if level == "low" and not reasons:
        reasons = ["目前可辨識的評論中未發現明顯誘導或重複訊號"]
    return level, reasons, signals, evidence


def _sentiment_from_evidence(evidence: List[Dict[str, Any]]) -> str:
    rows = [item for item in evidence if item.get("aspect") in ASPECT_DEFINITIONS]
    positive = sum(item.get("polarity") == "positive" for item in rows)
    negative = sum(item.get("polarity") == "negative" for item in rows)
    if not positive and not negative:
        return "unknown"
    if positive >= negative * 2 + 1:
        return "positive"
    if negative >= positive * 2 + 1:
        return "negative"
    return "mixed"


def _fallback_summary(
    aspects: Dict[str, Dict[str, Any]],
    evidence: List[Dict[str, Any]],
) -> Dict[str, Any]:
    scored = [(key, value) for key, value in aspects.items() if value.get("score") is not None]
    positive = sorted(scored, key=lambda item: item[1]["score"], reverse=True)
    negative = sorted(scored, key=lambda item: item[1]["score"])
    pros_evidence = []
    cons_evidence = []
    if positive and positive[0][1]["score"] >= 60:
        key, value = positive[0]
        pros_evidence.append(
            {"text": f"{value['label']}相關評價較正面", "evidenceIds": value["evidenceIds"][:3]}
        )
    if negative and negative[0][1]["score"] < 55:
        key, value = negative[0]
        cons_evidence.append(
            {"text": f"{value['label']}是較需要留意的面向", "evidenceIds": value["evidenceIds"][:3]}
        )
    if not scored:
        summary = "目前來源尚不足以形成可靠的面向分數，建議直接查看來源內容。"
    else:
        labels = "、".join(value["label"] for _, value in positive[:2])
        summary = f"目前可比較的面向以{labels}為主；請搭配資料信心與來源證據一起判斷。"
    return {
        "summary": summary,
        "prosEvidence": pros_evidence,
        "consEvidence": cons_evidence or [{"text": "部分面向資料仍有限", "evidenceIds": []}],
        "recommendedFor": ["想依公開證據比較餐廳的客人"],
    }


def _summarize_reviews(
    restaurant_name: str,
    aspects: Dict[str, Dict[str, Any]],
    evidence: List[Dict[str, Any]],
) -> Dict[str, Any]:
    fallback = _fallback_summary(aspects, evidence)
    usable = [item for item in evidence if item.get("aspect") in ASPECT_DEFINITIONS][:24]
    if not usable:
        return fallback
    try:
        from ollama_fuc import chat

        prompt = f"""
你是餐廳評價整理助手。只能根據以下證據輸出繁體中文 JSON，不得加入證據沒有提到的事實。
餐廳：{restaurant_name}
證據：{json.dumps(usable, ensure_ascii=False)}
面向分數：{json.dumps(aspects, ensure_ascii=False)}

只輸出 JSON：
summary: 100字內客觀摘要
prosEvidence: 最多3項，每項含 text 與 evidenceIds
consEvidence: 最多3項，每項含 text 與 evidenceIds
recommendedFor: 最多3個客群
每個 evidenceIds 只能使用上方存在的證據編號。
"""
        obj = _extract_json_object(chat([{"role": "user", "content": prompt}], timeout=45.0))
        if not obj:
            return fallback
        valid_ids = {str(item["evidenceId"]) for item in usable}

        def clean_highlights(value: Any, fallback_value: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            if not isinstance(value, list):
                return fallback_value
            rows = []
            for item in value[:3]:
                if not isinstance(item, dict) or not str(item.get("text") or "").strip():
                    continue
                ids = [str(item_id) for item_id in item.get("evidenceIds", []) if str(item_id) in valid_ids]
                rows.append({"text": str(item["text"])[:100], "evidenceIds": ids})
            return rows or fallback_value

        return {
            "summary": str(obj.get("summary") or fallback["summary"])[:220],
            "prosEvidence": clean_highlights(obj.get("prosEvidence"), fallback["prosEvidence"]),
            "consEvidence": clean_highlights(obj.get("consEvidence"), fallback["consEvidence"]),
            "recommendedFor": _string_list(
                obj.get("recommendedFor"), fallback["recommendedFor"], limit=3
            ),
        }
    except Exception:
        return fallback


def analyze_review_signals(sources: List[ReviewSource]) -> Dict[str, Any]:
    if not sources:
        return {
            "aspects": _empty_aspects(),
            "evidence": [],
            "recommendationScore": 0,
            "overallScore": 0,
            "scoreBasis": "aspects",
            "platformRating": None,
            "confidenceScore": 0,
            "riskLevel": "unknown",
            "riskReasons": ["公開評價來源不足，暫時無法判斷可信度"],
            "riskSignals": [],
            "sentiment": "unknown",
            "incentiveHits": [],
            "duplicateCount": 0,
        }
    rule_evidence = _extract_rule_evidence(sources)
    aspects = _calculate_aspects(rule_evidence)
    risk_level, risk_reasons, risk_signals, evidence = _risk_evidence(sources, rule_evidence)
    recommendation = _recommendation_score(aspects)
    platform_rating = _platform_rating(sources)
    score_basis = "aspects"
    if recommendation == 0 and platform_rating:
        recommendation = round(float(platform_rating["average"]) * 20)
        score_basis = "platform_rating"
    return {
        "aspects": aspects,
        "evidence": evidence,
        "recommendationScore": recommendation,
        "overallScore": recommendation,
        "scoreBasis": score_basis,
        "platformRating": platform_rating,
        "confidenceScore": 0,
        "riskLevel": risk_level,
        "riskReasons": risk_reasons,
        "riskSignals": risk_signals,
        "sentiment": _sentiment_from_evidence(evidence),
        "incentiveHits": sorted(
            {
                keyword
                for source in sources
                for keyword in INCENTIVE_KEYWORDS
                if keyword in f"{source.get('excerpt', '')} {source.get('content', '')}"
            }
        ),
        "duplicateCount": sum(bool(source.get("duplicateOf")) for source in sources),
    }


def _normalize_highlights(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        {
            "text": str(item.get("text") or "")[:100],
            "evidenceIds": [str(eid) for eid in item.get("evidenceIds", [])][:8],
        }
        for item in value
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ][:4]


def _normalize_review_report(
    data: Dict[str, Any],
    restaurant_name: str,
    default_success: bool = False,
) -> ReviewReport:
    legacy = int(data.get("schemaVersion") or 1) < SCHEMA_VERSION
    identity = _normalize_identity(data.get("restaurantIdentity"), restaurant_name)
    recommendation = (
        0
        if legacy
        else int(data.get("recommendationScore") or data.get("overallScore") or 0)
    )
    aspects = _empty_aspects()
    if isinstance(data.get("aspects"), dict):
        for key, default in aspects.items():
            incoming = data["aspects"].get(key)
            if not isinstance(incoming, dict):
                continue
            score = incoming.get("score")
            default.update(
                {
                    "label": str(incoming.get("label") or default["label"]),
                    "score": max(0, min(100, int(score))) if isinstance(score, (int, float)) else None,
                    "confidence": max(0, min(100, int(incoming.get("confidence") or 0))),
                    "mentionCount": max(0, int(incoming.get("mentionCount") or 0)),
                    "evidenceIds": [str(eid) for eid in incoming.get("evidenceIds", [])][:8],
                    "status": (
                        str(incoming.get("status"))
                        if incoming.get("status") in {"supported", "estimated", "insufficient"}
                        else ("supported" if isinstance(score, (int, float)) else "insufficient")
                    ),
                }
            )
    risk_level = "unknown" if legacy else str(data.get("riskLevel") or "unknown")
    if risk_level not in {"low", "medium", "high", "unknown"}:
        risk_level = "unknown"
    sources = []
    for index, source in enumerate(data.get("sources", [])):
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "")
        source_type, default_quality = _source_type_and_quality(url, str(source.get("title") or ""))
        provider = str(source.get("provider") or "legacy")
        if provider not in SEARCH_PROVIDER_NAMES:
            provider = "legacy"
        sources.append(
            {
                "sourceId": str(source.get("sourceId") or f"SRC-LEGACY-{index + 1:03d}"),
                "title": str(source.get("title") or "")[:160],
                "url": url,
                "canonicalUrl": str(source.get("canonicalUrl") or url),
                "excerpt": str(source.get("excerpt") or "")[:700],
                "sourceType": str(source.get("sourceType") or source_type),
                "publishedAt": source.get("publishedAt"),
                "retrievedAt": source.get("retrievedAt") or data.get("updatedAt"),
                "sourceQuality": float(source.get("sourceQuality", default_quality)),
                "sponsored": bool(source.get("sponsored")),
                "duplicateOf": source.get("duplicateOf"),
                "fetchStatus": str(source.get("fetchStatus") or "legacy"),
                "provider": provider,
                "rank": max(0, int(source.get("rank") or 0)),
                "platformRating": (
                    source.get("platformRating")
                    if isinstance(source.get("platformRating"), dict)
                    else None
                ),
            }
        )
    success = bool(data.get("success", default_success))
    incoming_search_meta = data.get("searchMeta")
    search_meta = _empty_search_meta()
    if isinstance(incoming_search_meta, dict):
        status = str(incoming_search_meta.get("status") or "no_relevant_sources")
        if status not in {
            "ok", "blocked", "browser_unavailable", "network_error",
            "no_relevant_sources",
        }:
            status = "no_relevant_sources"
        search_provider = str(incoming_search_meta.get("provider") or "")
        if search_provider not in SEARCH_PROVIDER_NAMES - {"legacy"}:
            search_provider = ""
        search_meta = {
            "status": status,
            "provider": search_provider or None,
            "attemptedProviders": [
                str(item) for item in incoming_search_meta.get("attemptedProviders", [])
                if str(item) in SEARCH_PROVIDER_NAMES - {"multi", "legacy"}
            ],
            "rawResultCount": max(0, int(incoming_search_meta.get("rawResultCount") or 0)),
            "relevantSourceCount": max(
                0, int(incoming_search_meta.get("relevantSourceCount") or 0)
            ),
        }
    return {
        "schemaVersion": SCHEMA_VERSION,
        "success": success,
        "message": str(data.get("message") or ("評價已更新" if success else "尚未更新評價")),
        "restaurantName": str(data.get("restaurantName") or restaurant_name),
        "restaurantIdentity": identity,
        "needsIdentity": identity is None,
        "needsRefresh": bool(data.get("needsRefresh")) or legacy or not success,
        "updatedAt": data.get("updatedAt"),
        "recommendationScore": max(0, min(100, recommendation)),
        "confidenceScore": max(0, min(100, int(data.get("confidenceScore") or 0))),
        "overallScore": max(0, min(100, recommendation)),
        "scoreBasis": (
            str(data.get("scoreBasis"))
            if data.get("scoreBasis") in {"aspects", "platform_rating"}
            else "aspects"
        ),
        "platformRating": data.get("platformRating") if isinstance(data.get("platformRating"), dict) else None,
        "sentiment": data.get("sentiment") if data.get("sentiment") in {"positive", "mixed", "negative", "unknown"} else "unknown",
        "summary": str(data.get("summary") or ""),
        "pros": _string_list(data.get("pros"), [], 4),
        "cons": _string_list(data.get("cons"), [], 4),
        "prosEvidence": _normalize_highlights(data.get("prosEvidence")),
        "consEvidence": _normalize_highlights(data.get("consEvidence")),
        "recommendedFor": _string_list(data.get("recommendedFor"), [], 3),
        "aspects": aspects,
        "riskLevel": risk_level,
        "riskReasons": _string_list(data.get("riskReasons"), [], 6),
        "riskSignals": _normalize_highlights(
            [
                {"text": signal.get("label"), "evidenceIds": signal.get("evidenceIds", [])}
                for signal in data.get("riskSignals", [])
                if isinstance(signal, dict)
            ]
        ),
        "evidence": [
            {
                "evidenceId": str(item.get("evidenceId") or ""),
                "sourceId": str(item.get("sourceId") or ""),
                "aspect": str(item.get("aspect") or ""),
                "polarity": str(item.get("polarity") or "neutral"),
                "text": str(item.get("text") or "")[:220],
                "weight": float(item.get("weight") or 0),
            }
            for item in data.get("evidence", [])
            if isinstance(item, dict)
        ],
        "sources": sources,
        "searchMeta": search_meta,
    }


def load_review_cache(project_root: str | Path, restaurant_name: str) -> ReviewReport:
    path = _cache_path(project_root, restaurant_name)
    if not path.exists():
        return _empty_review_report(restaurant_name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return _empty_review_report(restaurant_name, f"評價快取讀取失敗: {exc}")
    return _normalize_review_report(data, restaurant_name, default_success=True)


def write_review_cache(project_root: str | Path, restaurant_name: str, report: ReviewReport) -> None:
    path = _cache_path(project_root, restaurant_name)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def refresh_restaurant_reviews(
    project_root: str | Path,
    restaurant_name: str,
    restaurant_identity: Optional[Dict[str, Any]] = None,
    source_urls: Optional[List[str]] = None,
) -> ReviewReport:
    cached = load_review_cache(project_root, restaurant_name)
    identity = _normalize_identity(
        restaurant_identity or cached.get("restaurantIdentity"), restaurant_name
    )
    if restaurant_identity is None and identity and not identity.get("address"):
        normalized_identity_name = re.sub(
            r"\W+", "", str(identity.get("officialName") or "")
        ).lower()
        normalized_restaurant_name = re.sub(r"\W+", "", restaurant_name).lower()
        if normalized_identity_name == normalized_restaurant_name:
            identity = None
    if identity is None:
        candidates = identify_restaurant_candidates(restaurant_name)
        if candidates:
            top = candidates[0]
            second_confidence = (
                int(candidates[1].get("confidence") or 0) if len(candidates) > 1 else 0
            )
            top_confidence = int(top.get("confidence") or 0)
            name_similarity = SequenceMatcher(
                None,
                re.sub(r"\W+", "", restaurant_name).lower(),
                re.sub(r"\W+", "", str(top.get("officialName") or "")).lower(),
            ).ratio()
            can_auto_select = (
                top_confidence >= 70
                and (
                    bool(top.get("address"))
                    or top_confidence - second_confidence >= 8
                    or (top_confidence >= 78 and name_similarity >= 0.55)
                )
            )
            if can_auto_select:
                auto_identity = dict(top)
                auto_identity["confirmed"] = True
                auto_identity["queryName"] = restaurant_name
                identity = _normalize_identity(auto_identity, restaurant_name)
        if identity is None:
            report = _empty_review_report(
                restaurant_name,
                "找到多個可能分店，請選擇正確店家",
                identity=None,
            )
            report["needsIdentity"] = True
            report["identityCandidates"] = candidates
            return report

    sources, search_meta = _collect_enriched_sources(identity, source_urls)
    signals = analyze_review_signals(sources)
    aspects = signals["aspects"]
    recommendation = int(signals["recommendationScore"])
    confidence = _confidence_score(identity, sources, aspects)
    summary = _summarize_reviews(identity["officialName"], aspects, signals["evidence"])
    if signals.get("scoreBasis") == "platform_rating" and signals.get("platformRating"):
        rating = signals["platformRating"]
        review_count = rating.get("reviewCount")
        count_text = f"，約 {review_count} 則評分" if review_count else ""
        summary["summary"] = (
            f"平台顯示 {rating.get('average')}/5{count_text}；"
            "目前缺少可分析的文字內容，面向分數不做推測。"
        )
    pros_evidence = summary["prosEvidence"]
    cons_evidence = summary["consEvidence"]
    has_sources = bool(sources)
    report = _normalize_review_report(
        {
            "schemaVersion": SCHEMA_VERSION,
            "success": has_sources,
            "message": (
                "評價已更新"
                if has_sources
                else _search_status_message(str(search_meta.get("status") or ""))
            ),
            "restaurantName": restaurant_name,
            "restaurantIdentity": identity,
            "needsIdentity": False,
            "needsRefresh": not has_sources,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "recommendationScore": recommendation,
            "confidenceScore": confidence,
            "overallScore": recommendation,
            "scoreBasis": signals.get("scoreBasis", "aspects"),
            "platformRating": signals.get("platformRating"),
            "sentiment": signals["sentiment"],
            "summary": summary["summary"],
            "pros": [item["text"] for item in pros_evidence],
            "cons": [item["text"] for item in cons_evidence],
            "prosEvidence": pros_evidence,
            "consEvidence": cons_evidence,
            "recommendedFor": summary["recommendedFor"],
            "aspects": aspects,
            "riskLevel": signals["riskLevel"],
            "riskReasons": signals["riskReasons"],
            "riskSignals": signals["riskSignals"],
            "evidence": signals["evidence"],
            "sources": sources,
            "searchMeta": search_meta,
        },
        restaurant_name,
        default_success=has_sources,
    )
    write_review_cache(project_root, restaurant_name, report)
    return report
