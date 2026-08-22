import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import restaurant_reviews as reviews  # noqa: E402


def source(
    source_id: str,
    text: str,
    *,
    source_type: str = "forum",
    quality: float = 0.8,
    published_at: str | None = None,
) -> dict:
    return {
        "sourceId": source_id,
        "title": f"測試來源 {source_id}",
        "url": f"https://example{source_id[-1]}.com/review",
        "canonicalUrl": f"https://example{source_id[-1]}.com/review",
        "excerpt": text,
        "content": text,
        "sourceType": source_type,
        "publishedAt": published_at,
        "retrievedAt": datetime.now(timezone.utc).isoformat(),
        "sourceQuality": quality,
        "sponsored": False,
        "duplicateOf": None,
        "fetchStatus": "full",
    }


class RestaurantReviewsV3Test(unittest.TestCase):
    def setUp(self):
        self.ifoodie_collector = reviews._collect_review_sources_via_ifoodie
        self.ifoodie = mock.patch.object(
            reviews, "_collect_review_sources_via_ifoodie", return_value=[]
        )
        self.bing_rss = mock.patch.object(
            reviews, "_collect_review_sources_via_bing_rss", return_value=[]
        )
        self.google_news_rss = mock.patch.object(
            reviews, "_collect_review_sources_via_google_news_rss", return_value=[]
        )
        self.google_maps = mock.patch.object(
            reviews,
            "_playwright_google_maps_profile",
            new=mock.AsyncMock(return_value=[]),
        )
        self.ifoodie.start()
        self.bing_rss.start()
        self.google_news_rss.start()
        self.google_maps.start()

    def tearDown(self):
        self.google_maps.stop()
        self.google_news_rss.stop()
        self.bing_rss.stop()
        self.ifoodie.stop()

    def test_missing_cache_returns_v3_unknown_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = reviews.load_review_cache(tmp, "不存在餐廳")

        self.assertEqual(report["schemaVersion"], 3)
        self.assertFalse(report["success"])
        self.assertTrue(report["needsIdentity"])
        self.assertEqual(report["riskLevel"], "unknown")
        self.assertEqual(report["recommendationScore"], 0)
        self.assertEqual(report["confidenceScore"], 0)
        self.assertTrue(report["needsRefresh"])
        self.assertEqual(report["searchMeta"]["status"], "no_relevant_sources")

    def test_legacy_cache_is_readable_and_marked_for_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            reviews.write_review_cache(
                tmp,
                "測試餐廳",
                {
                    "success": True,
                    "restaurantName": "測試餐廳",
                    "overallScore": 78,
                    "riskLevel": "low",
                    "sources": [
                        {
                            "title": "舊來源",
                            "url": "https://example.com/a",
                            "excerpt": "牛肉麵好吃",
                            "sourceType": "web",
                        }
                    ],
                },
            )
            report = reviews.load_review_cache(tmp, "測試餐廳")

        self.assertTrue(report["success"])
        self.assertTrue(report["needsRefresh"])
        self.assertEqual(report["recommendationScore"], 0)
        self.assertEqual(report["overallScore"], 0)
        self.assertEqual(report["riskLevel"], "unknown")
        self.assertEqual(report["sources"][0]["fetchStatus"], "legacy")

    def test_refresh_requires_confirmed_identity_before_collecting(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                reviews,
                "identify_restaurant_candidates",
                return_value=[
                    {
                        "officialName": "測試餐廳 A店",
                        "address": "",
                        "confidence": 55,
                    },
                    {
                        "officialName": "測試餐廳 B店",
                        "address": "",
                        "confidence": 53,
                    },
                ],
            ):
                with mock.patch.object(reviews, "_collect_enriched_sources") as collect:
                    report = reviews.refresh_restaurant_reviews(tmp, "測試餐廳")

        collect.assert_not_called()
        self.assertTrue(report["needsIdentity"])
        self.assertFalse(report["success"])
        self.assertEqual(len(report["identityCandidates"]), 2)

    def test_identify_candidates_extracts_address_and_deduplicates(self):
        raw = [
            {
                "title": "測試餐廳 台中店｜地址與評價",
                "url": "https://example.com/store",
                "excerpt": "地址 407台中市西屯區台灣大道三段100號",
                "sourceType": "web",
            },
            {
                "title": "測試餐廳 台中店｜地址與評價",
                "url": "https://example.com/store-copy",
                "excerpt": "地址 407台中市西屯區台灣大道三段100號",
                "sourceType": "web",
            },
        ]
        with mock.patch.object(
            reviews,
            "_search_public_web",
            new=mock.AsyncMock(return_value=(raw, reviews._empty_search_meta("ok"))),
        ):
            candidates = reviews.identify_restaurant_candidates("測試餐廳 台中店")

        self.assertEqual(len(candidates), 1)
        self.assertIn("台中市", candidates[0]["address"])
        self.assertIn("google.com/maps", candidates[0]["mapsUrl"])

    def test_canonical_url_removes_tracking_and_fragment(self):
        url = reviews._canonicalize_url(
            "https://Example.com/review/?id=7&utm_source=test&fbclid=x#comments"
        )
        self.assertEqual(url, "https://example.com/review?id=7")

    def test_full_page_failure_falls_back_to_search_snippet(self):
        identity = {"officialName": "測試餐廳", "address": "", "confirmed": True}
        raw = {
            "title": "測試餐廳評價",
            "url": "https://example.com/review",
            "excerpt": "牛肉麵好吃，份量充足。",
        }
        with mock.patch.object(reviews, "_extract_page", side_effect=OSError("blocked")):
            enriched = reviews._enrich_source(raw, identity)

        self.assertEqual(enriched["fetchStatus"], "snippet")
        self.assertIn("牛肉麵好吃", enriched["content"])

    def test_duplicate_content_is_excluded_from_quality(self):
        sources = [
            source("SRC-1", "牛肉麵很好吃，份量充足，服務人員也很親切。" * 5),
            source("SRC-2", "牛肉麵很好吃，份量充足，服務人員也很親切。" * 5),
        ]
        reviews._mark_duplicates(sources)

        self.assertEqual(sources[1]["duplicateOf"], "SRC-1")
        self.assertEqual(sources[1]["sourceQuality"], 0)

    def test_recency_weight_decreases_for_old_and_unknown_sources(self):
        recent = datetime.now(timezone.utc) - timedelta(days=30)
        old = datetime.now(timezone.utc) - timedelta(days=1500)

        self.assertEqual(reviews._recency_weight(recent.isoformat()), 1.0)
        self.assertEqual(reviews._recency_weight(old.isoformat()), 0.4)
        self.assertEqual(reviews._recency_weight(None), 0.6)

    def test_aspect_one_source_is_estimated_and_two_are_supported(self):
        one_source = [
            source("SRC-1", "這間店的牛肉麵味道很好吃，湯頭也很美味。")
        ]
        result = reviews.analyze_review_signals(one_source)
        self.assertIsInstance(result["aspects"]["taste"]["score"], int)
        self.assertEqual(result["aspects"]["taste"]["status"], "estimated")
        one_source_confidence = result["aspects"]["taste"]["confidence"]

        two_sources = one_source + [
            source("SRC-2", "餐點口味不錯而且很新鮮，會想再次回訪。")
        ]
        result = reviews.analyze_review_signals(two_sources)
        self.assertIsInstance(result["aspects"]["taste"]["score"], int)
        self.assertEqual(result["aspects"]["taste"]["status"], "supported")
        self.assertGreater(
            result["aspects"]["taste"]["confidence"], one_source_confidence
        )
        self.assertGreater(result["recommendationScore"], 0)

    def test_refresh_auto_selects_clear_identity_candidate(self):
        candidate = {
            "officialName": "測試餐廳 台中店",
            "address": "台中市西屯區測試路1號",
            "confidence": 88,
        }
        fake_sources = [
            source("SRC-1", "牛肉麵口味好吃，價格也很划算。"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                reviews, "identify_restaurant_candidates", return_value=[candidate]
            ):
                with mock.patch.object(
                    reviews,
                    "_collect_enriched_sources",
                    return_value=(
                        fake_sources,
                        {
                            "status": "ok",
                            "provider": "http",
                            "attemptedProviders": ["http"],
                            "rawResultCount": 2,
                            "relevantSourceCount": 1,
                        },
                    ),
                ):
                    with mock.patch.object(
                        reviews,
                        "_summarize_reviews",
                        return_value={
                            "summary": "有初步正面評價。",
                            "prosEvidence": [],
                            "consEvidence": [],
                            "recommendedFor": [],
                        },
                    ):
                        report = reviews.refresh_restaurant_reviews(
                            tmp, "測試餐廳"
                        )

        self.assertFalse(report["needsIdentity"])
        self.assertEqual(
            report["restaurantIdentity"]["officialName"], candidate["officialName"]
        )

    def test_incentive_risk_has_traceable_evidence(self):
        sources = [
            source("SRC-1", "店內公告五星送飲料，評論送小菜。"),
            source("SRC-2", "餐點味道普通，但服務人員親切。"),
        ]
        result = reviews.analyze_review_signals(sources)

        self.assertIn(result["riskLevel"], {"medium", "high"})
        self.assertIn("五星送", result["incentiveHits"])
        ids = result["riskSignals"][0]["evidenceIds"]
        evidence_ids = {item["evidenceId"] for item in result["evidence"]}
        self.assertTrue(ids)
        self.assertTrue(set(ids).issubset(evidence_ids))

    def test_sparse_non_review_sources_report_unknown_risk(self):
        sources = [
            source(
                "SRC-1",
                "官方網站提供餐廳地址與營業時間。",
                source_type="web",
                quality=0.4,
            )
        ]
        result = reviews.analyze_review_signals(sources)
        self.assertEqual(result["riskLevel"], "unknown")

    def test_platform_rating_provides_labeled_fallback_score(self):
        sources = [
            source(
                "SRC-1",
                "Foodpanda 4.9/5，4000+ 則評論。",
                source_type="review_platform",
                quality=0.9,
            )
        ]
        result = reviews.analyze_review_signals(sources)

        self.assertEqual(result["recommendationScore"], 98)
        self.assertEqual(result["scoreBasis"], "platform_rating")
        self.assertEqual(result["platformRating"]["average"], 4.9)

    def test_refresh_builds_separate_recommendation_and_confidence_scores(self):
        identity = {
            "officialName": "測試餐廳 台中店",
            "address": "台中市西屯區測試路1號",
            "confirmed": True,
        }
        fake_sources = [
            source("SRC-1", "牛肉麵味道好吃，價格也很划算。"),
            source("SRC-2", "餐點口味美味，價位便宜值得回訪。"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                reviews,
                "_collect_enriched_sources",
                return_value=(
                    fake_sources,
                    {
                        "status": "ok",
                        "provider": "http",
                        "attemptedProviders": ["http"],
                        "rawResultCount": 2,
                        "relevantSourceCount": 2,
                    },
                ),
            ):
                with mock.patch.object(
                    reviews,
                    "_summarize_reviews",
                    return_value={
                        "summary": "口味與價格有正面證據。",
                        "prosEvidence": [],
                        "consEvidence": [],
                        "recommendedFor": [],
                    },
                ):
                    report = reviews.refresh_restaurant_reviews(
                        tmp, "測試餐廳", identity
                    )

            cached = reviews.load_review_cache(tmp, "測試餐廳")

        self.assertTrue(report["success"])
        self.assertFalse(report["needsIdentity"])
        self.assertEqual(report["overallScore"], report["recommendationScore"])
        self.assertGreater(report["confidenceScore"], 0)
        self.assertEqual(cached["restaurantIdentity"]["address"], identity["address"])
        self.assertEqual(cached["schemaVersion"], 3)
        self.assertEqual(cached["searchMeta"]["provider"], "http")

    def test_rss_mode_uses_http_without_starting_playwright(self):
        http_rows = [
            {
                "title": "測試餐廳食記",
                "url": "https://food.example/review",
                "excerpt": "餐點好吃且服務親切。",
            }
        ]
        with mock.patch.dict(
            "os.environ",
            {"REVIEW_SEARCH_MODE": "rss", "USE_PLAYWRIGHT_REVIEW_SEARCH": "false"},
            clear=False,
        ):
            with mock.patch.object(
                reviews, "_playwright_search_queries", new=mock.AsyncMock()
            ) as browser_search:
                with mock.patch.object(
                    reviews,
                    "_collect_review_sources_via_html_search",
                    return_value=http_rows,
                ):
                    rows, meta = asyncio.run(
                        reviews._search_public_web(["測試餐廳 評價"])
                    )

        browser_search.assert_not_awaited()
        self.assertEqual(rows[0]["provider"], "http")
        self.assertEqual(meta["provider"], "http")
        self.assertEqual(
            meta["attemptedProviders"],
            ["ifoodie", "bing_rss", "google_news_rss", "http"],
        )

    def test_enabled_playwright_with_sparse_results_still_calls_http_fallback(self):
        browser_rows = [
            {
                "title": "測試餐廳部落格",
                "url": "https://blog.example/review",
                "excerpt": "測試餐廳餐點好吃。",
            }
        ]
        with mock.patch.dict(
            "os.environ", {"USE_PLAYWRIGHT_REVIEW_SEARCH": "true"}, clear=False
        ):
            with mock.patch.object(
                reviews,
                "_playwright_search_queries",
                new=mock.AsyncMock(return_value=browser_rows),
            ):
                with mock.patch.object(
                    reviews, "_collect_review_sources_via_html_search", return_value=[]
                ) as http_search:
                    rows, meta = asyncio.run(
                        reviews._search_public_web(["測試餐廳 評價"])
                    )

        http_search.assert_called_once()
        self.assertEqual(rows[0]["provider"], "playwright")
        self.assertEqual(meta["provider"], "playwright")
        self.assertEqual(
            meta["attemptedProviders"],
            ["google_maps", "ifoodie", "bing_rss", "google_news_rss", "playwright", "http"],
        )

    def test_playwright_failure_falls_back_to_http(self):
        http_rows = [
            {
                "title": "測試餐廳論壇心得",
                "url": "https://forum.example/review",
                "excerpt": "測試餐廳價格合理。",
            }
        ]
        with mock.patch.dict(
            "os.environ", {"USE_PLAYWRIGHT_REVIEW_SEARCH": "true"}, clear=False
        ):
            with mock.patch.object(
                reviews,
                "_playwright_search_queries",
                new=mock.AsyncMock(
                    side_effect=reviews.SearchProviderError("browser_unavailable")
                ),
            ):
                with mock.patch.object(
                    reviews,
                    "_collect_review_sources_via_html_search",
                    return_value=http_rows,
                ):
                    rows, meta = asyncio.run(
                        reviews._search_public_web(["測試餐廳 評價"])
                    )

        self.assertEqual(rows[0]["provider"], "http")
        self.assertEqual(meta["status"], "ok")
        self.assertEqual(
            meta["attemptedProviders"],
            ["google_maps", "ifoodie", "bing_rss", "google_news_rss", "playwright", "http"],
        )

    def test_playwright_unavailable_is_reported_when_http_has_no_results(self):
        with mock.patch.dict(
            "os.environ", {"USE_PLAYWRIGHT_REVIEW_SEARCH": "true"}, clear=False
        ):
            with mock.patch.object(
                reviews,
                "_playwright_search_queries",
                new=mock.AsyncMock(
                    side_effect=reviews.SearchProviderError("browser_unavailable")
                ),
            ):
                with mock.patch.object(
                    reviews,
                    "_collect_review_sources_via_html_search",
                    return_value=[],
                ):
                    rows, meta = asyncio.run(
                        reviews._search_public_web(["測試餐廳 評價"])
                    )

        self.assertEqual(rows, [])
        self.assertEqual(meta["status"], "browser_unavailable")
        self.assertEqual(
            meta["attemptedProviders"],
            ["google_maps", "ifoodie", "bing_rss", "google_news_rss", "playwright", "http"],
        )

    def test_http_search_uses_jina_then_duckduckgo_then_bing(self):
        fetched_urls = []

        def fake_fetch(url):
            fetched_urls.append(url)
            if url.startswith("https://r.jina.ai/"):
                return "[Jina 食記](https://jina.example/review)\n餐點好吃而且服務親切。"
            if "duckduckgo.com" in url:
                return (
                    '<a class="result__a" href="https://duck.example/review">Duck 食記</a>'
                    '<div class="result__snippet">價格合理而且份量充足。</div>'
                )
            return (
                '<li class="b_algo"><a href="https://bing.example/review">Bing 食記</a>'
                '<p>環境乾淨而且座位舒適。</p></li>'
            )

        with mock.patch.object(reviews, "_fetch_search_html", side_effect=fake_fetch):
            rows = reviews._collect_review_sources_via_html_search(
                ["測試餐廳 評價"], set()
            )

        self.assertEqual(len(fetched_urls), 3)
        self.assertTrue(fetched_urls[0].startswith("https://r.jina.ai/"))
        self.assertIn("duckduckgo.com", fetched_urls[1])
        self.assertIn("bing.com", fetched_urls[2])
        self.assertEqual(
            [row["url"] for row in rows],
            [
                "https://jina.example/review",
                "https://duck.example/review",
                "https://bing.example/review",
            ],
        )

    def test_http_network_failure_is_exposed_in_search_meta(self):
        def fail_http(_queries, _seen, diagnostics=None, limit=12):
            diagnostics.append("network_error")
            return []

        with mock.patch.dict(
            "os.environ",
            {"REVIEW_SEARCH_MODE": "rss", "USE_PLAYWRIGHT_REVIEW_SEARCH": "false"},
            clear=False,
        ):
            with mock.patch.object(
                reviews,
                "_collect_review_sources_via_html_search",
                side_effect=fail_http,
            ):
                rows, meta = asyncio.run(
                    reviews._search_public_web(["測試餐廳 評價"])
                )

        self.assertEqual(rows, [])
        self.assertEqual(meta["status"], "network_error")
        self.assertEqual(
            meta["attemptedProviders"],
            ["ifoodie", "bing_rss", "google_news_rss", "http"],
        )

    def test_bing_rss_parser_reads_public_review_items(self):
        rss = """<?xml version="1.0" encoding="utf-8"?>
        <rss><channel><item>
          <title>測試餐廳食記｜餐點與服務心得</title>
          <link>https://food.example/review?utm_source=bing</link>
          <description><![CDATA[餐點好吃，服務也很親切。]]></description>
          <pubDate>Sun, 02 Aug 2026 08:00:00 GMT</pubDate>
        </item></channel></rss>"""

        rows = reviews._parse_rss_search_results(rss, "bing_rss")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "測試餐廳食記｜餐點與服務心得")
        self.assertIn("服務也很親切", rows[0]["excerpt"])
        self.assertTrue(rows[0]["publishedAt"].startswith("2026-08-02"))

    def test_ifoodie_site_search_extracts_restaurant_page(self):
        html = """
        <div class="restaurant-card">
          <a href="/restaurant/abc-測試餐廳-東海店">測試餐廳 東海店</a>
          <p>台中市龍井區，4.6 分，共 12 則評論。</p>
        </div>
        """
        with mock.patch.object(reviews, "_fetch_search_html", return_value=html):
            rows = self.ifoodie_collector(["測試餐廳 東海店"])

        self.assertEqual(len(rows), 1)
        self.assertIn("ifoodie.tw/restaurant/", rows[0]["url"])
        self.assertIn("12 則評論", rows[0]["excerpt"])

    def test_free_rss_results_skip_legacy_html_when_enough(self):
        rss_rows = [
            {
                "title": f"測試餐廳公開食記 {index}",
                "url": f"https://food{index}.example/review",
                "excerpt": "測試餐廳餐點好吃且服務親切。",
            }
            for index in range(4)
        ]
        with mock.patch.object(
            reviews, "_collect_review_sources_via_bing_rss", return_value=rss_rows
        ):
            with mock.patch.object(
                reviews, "_collect_review_sources_via_html_search"
            ) as legacy_html:
                rows, meta = asyncio.run(
                    reviews._search_public_web(["測試餐廳 評價"])
                )

        legacy_html.assert_not_called()
        self.assertEqual(len(rows), 4)
        self.assertEqual(meta["provider"], "bing_rss")
        self.assertEqual(
            meta["attemptedProviders"],
            ["google_maps", "ifoodie", "bing_rss", "google_news_rss"],
        )

    def test_google_maps_profile_parser_extracts_public_rating(self):
        row = reviews._parse_google_maps_profile(
            "森森燒肉 台中中科店",
            "https://www.google.com/maps/place/example",
            "森森燒肉 台中中科店 - Google 地圖",
            "森森燒肉 台中中科店\n4.9\n407台中市西屯區台灣大道四段1316號",
            ["4.9 顆星", "1,234 則評論"],
        )

        self.assertIsNotNone(row)
        self.assertEqual(row["provider"], "google_maps")
        self.assertEqual(row["platformRating"]["average"], 4.9)
        self.assertEqual(row["platformRating"]["reviewCount"], 1234)
        self.assertIn("台中市西屯區", row["excerpt"])

    def test_explicit_google_rating_is_used_without_fabricating_review_count(self):
        row = source("SRC-GOOGLE", "Google 地圖公開頁顯示評分 4.7/5")
        row["provider"] = "google_maps"
        row["platformRating"] = {
            "average": 4.7,
            "reviewCount": None,
            "platform": "Google Maps",
        }

        blog_row = source("SRC-BLOG", "部落格文章中的其他數字 5/5")

        rating = reviews._platform_rating([row, blog_row])

        self.assertEqual(rating["average"], 4.7)
        self.assertIsNone(rating["reviewCount"])
        self.assertEqual(rating["platform"], "Google Maps")
        self.assertEqual(rating["sourceIds"], ["SRC-GOOGLE"])

    def test_v3_cache_preserves_free_provider_diagnostics(self):
        report = reviews._normalize_review_report(
            {
                "schemaVersion": 3,
                "success": True,
                "restaurantName": "測試餐廳",
                "restaurantIdentity": {"officialName": "測試餐廳", "confirmed": True},
                "sources": [
                    {
                        "title": "測試餐廳食記",
                        "url": "https://food.example/review",
                        "excerpt": "餐點好吃。",
                        "provider": "bing_rss",
                    }
                ],
                "searchMeta": {
                    "status": "ok",
                    "provider": "bing_rss",
                    "attemptedProviders": ["bing_rss", "google_news_rss"],
                    "rawResultCount": 5,
                    "relevantSourceCount": 1,
                },
            },
            "測試餐廳",
        )

        self.assertEqual(report["sources"][0]["provider"], "bing_rss")
        self.assertEqual(
            report["searchMeta"]["attemptedProviders"],
            ["bing_rss", "google_news_rss"],
        )

    def test_wrong_restaurant_branch_is_rejected(self):
        identity = {
            "officialName": "森森燒肉 台中中科店",
            "queryName": "森森燒肉_台中中科店",
            "address": "台中市西屯區台灣大道四段1316號",
        }
        wrong_branch = {
            "title": "森森燒肉 高雄台鋁店完整食記",
            "excerpt": "高雄聚餐與甜點吃到飽心得。",
            "content": "文章末尾列出森森燒肉台中中科店等其他分店。",
        }

        self.assertFalse(reviews._source_relevant(wrong_branch, identity))

    def test_menu_name_aliases_include_public_restaurant_name(self):
        names = reviews._restaurant_search_names(
            {
                "officialName": "奔頂牛排 東海",
                "queryName": "奔頂牛排_東海",
            }
        )

        self.assertIn("犇頂牛排 PLUS+ 東海店", names)

    def test_user_provided_public_url_is_added_as_source(self):
        identity = {
            "officialName": "測試餐廳",
            "queryName": "測試餐廳",
            "address": "",
        }
        rows = reviews._direct_source_rows(
            identity,
            ["https://www.dcard.tw/f/food/p/123?utm_source=test"],
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider"], "user_url")
        self.assertEqual(rows[0]["url"], "https://www.dcard.tw/f/food/p/123")

    def test_booking_and_social_pages_are_not_review_candidates(self):
        self.assertFalse(
            reviews._is_review_candidate(
                {
                    "title": "立即訂位",
                    "url": "https://inline.app/booking/example",
                }
            )
        )
        self.assertFalse(
            reviews._is_review_candidate(
                {
                    "title": "餐廳 Facebook",
                    "url": "https://www.facebook.com/example",
                }
            )
        )

    def test_search_results_are_canonicalized_and_deduplicated(self):
        rows = []
        seen = set()
        raw = [
            {"title": "食記 A", "url": "https://example.com/a?utm_source=x", "excerpt": "好吃"},
            {"title": "食記 A 複本", "url": "https://example.com/a", "excerpt": "好吃"},
        ]
        count = reviews._append_unique_search_rows(rows, raw, seen, "http")

        self.assertEqual(count, 2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://example.com/a")

    def test_search_result_limit_excludes_youtube(self):
        rows = []
        raw = [
            {
                "title": "YouTube 影片",
                "url": "https://www.youtube.com/watch?v=123",
                "excerpt": "影片評論",
            }
        ] + [
            {
                "title": f"公開食記 {index}",
                "url": f"https://food{index}.example/review",
                "excerpt": "餐點好吃且服務親切。",
            }
            for index in range(15)
        ]

        reviews._append_unique_search_rows(rows, raw, set(), "http", limit=12)

        self.assertEqual(len(rows), 12)
        self.assertTrue(all("youtube.com" not in row["url"] for row in rows))

    def test_llm_failure_uses_traceable_fallback_summary(self):
        signals = reviews.analyze_review_signals(
            [source("SRC-1", "牛肉麵口味好吃，價格也很划算。")]
        )
        with mock.patch("ollama_fuc.chat", side_effect=RuntimeError("offline")):
            summary = reviews._summarize_reviews(
                "測試餐廳", signals["aspects"], signals["evidence"]
            )

        evidence_ids = {item["evidenceId"] for item in signals["evidence"]}
        referenced_ids = {
            evidence_id
            for key in ("prosEvidence", "consEvidence")
            for item in summary[key]
            for evidence_id in item["evidenceIds"]
        }
        self.assertTrue(summary["summary"])
        self.assertTrue(referenced_ids.issubset(evidence_ids))

    def test_short_vague_praise_has_traceable_risk_evidence(self):
        result = reviews.analyze_review_signals(
            [source("SRC-1", "超讚，五星推薦！", source_type="forum")]
        )

        labels = [signal["label"] for signal in result["riskSignals"]]
        self.assertTrue(any("短句正面評價" in label for label in labels))
        self.assertTrue(result["riskSignals"][0]["evidenceIds"])


if __name__ == "__main__":
    unittest.main()
