import json
import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import back  # noqa: E402


class PWAAssetTest(unittest.TestCase):
    """加到主畫面需要的東西。壞掉時不會有任何錯誤訊息，只是安裝選項消失。"""

    def setUp(self):
        self.client = TestClient(back.app)

    def test_service_worker_is_served_from_the_site_root(self):
        # 放在 /web/sw.js 的話 scope 只有 /web，首頁不在裡面，裝起來仍然是一般分頁。
        response = self.client.get("/sw.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response.headers["content-type"])

    def test_service_worker_never_caches_api_responses(self):
        # 過期的菜單或推薦比錯誤訊息更難發現。
        source = (ROOT / "web" / "sw.js").read_text(encoding="utf-8")
        self.assertIn("/api/", source)
        self.assertIn("url.pathname.startsWith('/api/')", source)

    def test_manifest_has_what_install_requires(self):
        manifest = json.loads((ROOT / "web" / "manifest.webmanifest").read_text(encoding="utf-8"))
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["scope"], "/")
        self.assertEqual(manifest["display"], "standalone")
        sizes = {icon["sizes"] for icon in manifest["icons"]}
        self.assertIn("192x192", sizes)
        self.assertIn("512x512", sizes)
        purposes = {icon.get("purpose") for icon in manifest["icons"]}
        self.assertIn("maskable", purposes, "缺 maskable 圖示，Android 會把圖示塞進白底方框")

    def test_every_icon_the_manifest_promises_exists(self):
        manifest = json.loads((ROOT / "web" / "manifest.webmanifest").read_text(encoding="utf-8"))
        for icon in manifest["icons"]:
            with self.subTest(icon=icon["src"]):
                response = self.client.get(icon["src"])
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-type"], "image/png")

    def test_page_links_the_manifest_and_apple_icon(self):
        html = (ROOT / "web" / "web.html").read_text(encoding="utf-8")
        self.assertIn('rel="manifest"', html)
        self.assertIn('rel="apple-touch-icon"', html)  # iOS 不讀 manifest 的圖示
        self.assertIn('name="theme-color"', html)


if __name__ == "__main__":
    unittest.main()
