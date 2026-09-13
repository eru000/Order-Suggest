import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend routing tests")
class ChatRoutingTest(unittest.TestCase):
    def test_text_uses_chat_transport_even_with_an_active_decision(self):
        source = (ROOT / "web" / "web.js").read_text(encoding="utf-8")
        send = source.split("  const send = async () => {", 1)[1].split(
            "  const clearActiveChat", 1
        )[0]
        script = "const send = async () => {" + send + "\n" + """
(async () => {
  for (const available of [true, false]) {
    input.value = '這個會辣嗎？';
    streamAvailable = available;
    await send();
    if (input.disabled || input.value !== '') throw Error('input did not recover');
  }
  if (JSON.stringify(calls) !== JSON.stringify([
    ['stream', 'session', '這個會辣嗎？'],
    ['stream', 'session', '這個會辣嗎？'],
    ['sync', 'session', '這個會辣嗎？'],
  ])) throw Error(JSON.stringify(calls));
})().catch(e => { console.error(e); process.exitCode = 1; });
"""
        setup = """
const calls = [];
let streamAvailable = true;
const input = {value: '', disabled: false, focus() {}};
const getActiveThread = () => ({sessionId: 'session', decision: {revision: 'selected'}});
const appendMessage = () => {};
const showLoading = () => {};
const sendDecision = () => { throw Error('question incorrectly sent to decision endpoint'); };
const sendStreaming = async (...args) => { calls.push(['stream', ...args]); return streamAvailable; };
const sendBlocking = async (...args) => { calls.push(['sync', ...args]); };
"""
        result = subprocess.run(
            ["node", "-e", setup + script], capture_output=True, text=True, encoding="utf-8"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
