import json
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


if __name__ == "__main__":
    unittest.main()
