import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend scroll tests")
class ConversationScrollTest(unittest.TestCase):
    def test_reply_is_the_only_automatic_reading_anchor(self):
        module_path = json.dumps(str(ROOT / "web" / "conversation-scroll.js"))
        script = f"""
global.window = {{}};
global.cancelAnimationFrame = () => {{}};
global.requestAnimationFrame = callback => {{ callback(); return 1; }};
global.ResizeObserver = class {{
  constructor(callback) {{ this.callback = callback; }}
  observe() {{}}
  unobserve() {{}}
}};

const listeners = {{}};
const box = {{
  style: {{ paddingBottom: '' }},
  closest: () => owner,
  getBoundingClientRect: () => ({{
    bottom: reply.getBoundingClientRect().bottom + (parseFloat(box.style.paddingBottom) || 0),
  }}),
}};
let naturalHeight = 900;
const owner = {{
  clientHeight: 500,
  scrollTop: 200,
  get scrollHeight() {{
    return naturalHeight + (parseFloat(box.style.paddingBottom) || 0);
  }},
  addEventListener: (type, callback) => {{ listeners[type] = callback; }},
  getBoundingClientRect: () => ({{ top: 100 }}),
  scrollTo: (options) => {{ owner.scrollTop = options.top; }},
}};
const reply = {{
  isConnected: true,
  offsetHeight: 100,
  getBoundingClientRect: () => ({{
    top: 100 + 800 - owner.scrollTop,
    bottom: 100 + 900 - owner.scrollTop,
  }}),
}};

require({module_path});
const scroll = window.ConversationScroll.create(box);
scroll.begin();
if (owner.scrollTop !== 200) throw new Error('begin moved the conversation');
scroll.reveal(reply);
if (owner.scrollTop !== 788) throw new Error('reply was not aligned to its start');
if (box.style.paddingBottom !== '388px') throw new Error('reply lacks trailing reading space');

reply.offsetHeight = 400;
naturalHeight = 1200;
scroll.refresh();
if (owner.scrollTop !== 788) throw new Error('streaming growth moved the reading position');
if (box.style.paddingBottom !== '88px') throw new Error('trailing reading space was not resized');

owner.scrollTop = 300;
scroll.begin();
listeners.wheel();
scroll.reveal(reply);
if (owner.scrollTop !== 300) throw new Error('manual scrolling was overridden');
"""
        result = subprocess.run(
            ["node", "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
