import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend security tests")
class FrontendSecurityTest(unittest.TestCase):
    def test_chat_formatter_escapes_executable_html(self):
        module_path = json.dumps(str(ROOT / "web" / "format.js"))
        script = (
            f"const f=require({module_path});"
            "process.stdout.write(f.formatText('<img src=x onerror=alert(1)> **安全**'));"
        )
        output = subprocess.check_output(["node", "-e", script], text=True, encoding="utf-8")
        self.assertNotIn("<img", output)
        self.assertIn("&lt;img", output)
        self.assertIn("<strong>安全</strong>", output)


class FrontendSecretsTest(unittest.TestCase):
    """前端原始碼不得內嵌任何憑證。

    這條規則是有代價才學到的：曾經為了「不要每次都跳窗問金鑰」，把
    ADMIN_API_KEY 直接寫死在 web.js 裡。那支檔案會原封不動送到每個訪客的
    瀏覽器，而且這是公開 repo，commit 一次就永久留在歷史裡。真正的修法是
    把金鑰存進使用者自己的 localStorage，問一次就記住。
    """

    # 32 bytes 以上的十六進位字串，就是 ADMIN_API_KEY 那種格式
    HEX_SECRET = re.compile(r"['\"][0-9a-f]{32,}['\"]", re.I)
    # 常見的雲端服務金鑰前綴
    PREFIXED_SECRET = re.compile(r"['\"](sk-|ghp_|gho_|xox[baprs]-|AIza)[A-Za-z0-9_-]{10,}['\"]")

    def test_web_sources_contain_no_embedded_credentials(self):
        for path in sorted((ROOT / "web").glob("*.js")):
            source = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertIsNone(
                    self.HEX_SECRET.search(source),
                    f"{path.name} 內嵌了看起來像金鑰的十六進位字串",
                )
                self.assertIsNone(
                    self.PREFIXED_SECRET.search(source),
                    f"{path.name} 內嵌了看起來像 API 金鑰的字串",
                )

    def test_admin_key_is_read_from_browser_storage(self):
        source = (ROOT / "web" / "web.js").read_text(encoding="utf-8")
        self.assertIn("localStorage.getItem(ADMIN_KEY_STORAGE)", source)
        self.assertNotRegex(
            source,
            r"const\s+ADMIN_KEY\s*=\s*['\"][^'\"]+['\"]",
            "金鑰不可以是原始碼裡的常數",
        )


if __name__ == "__main__":
    unittest.main()
