import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend routing tests")
class ChatRoutingTest(unittest.TestCase):
    def test_new_and_cleared_chats_keep_the_selected_restaurant(self):
        source = (ROOT / "web" / "web.js").read_text(encoding="utf-8")
        start = source.index("  const clearActiveChat = ")
        end = source.index("  const crawlRestaurantMenu = ", start)
        setup = """
let nextSession = 0;
const sidFactory = () => 'session-' + (++nextSession);
let thread = {sessionId: sidFactory(), restaurantName: '筷子餐廳'};
const createThread = () => { thread = {sessionId: sidFactory()}; };
const getActiveThread = () => thread;
const restaurantSyncing = false;
const chatBox = {};
const input = {focus() {}};
const showLoading = () => {};
const appendMessage = () => {};
const saveState = () => {};
const renderChatList = () => {};
const nowTs = Date.now;
const switches = [];
const switchRestaurant = name => {
  switches.push([thread.sessionId, name]);
  thread.restaurantName = name;
};
const loadRestaurants = () => { throw Error('fell back to the default restaurant'); };
"""
        assertions = """
newChat();
clearActiveChat();
const expected = [['session-2', '筷子餐廳'], ['session-3', '筷子餐廳']];
if (JSON.stringify(switches) !== JSON.stringify(expected)) throw Error(JSON.stringify(switches));
"""
        result = subprocess.run(
            ["node", "-e", setup + source[start:end] + assertions],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_text_uses_chat_transport_even_with_an_active_decision(self):
        source = (ROOT / "web" / "web.js").read_text(encoding="utf-8")
        send = source.split("  const send = async () => {", 1)[1].split(
            "  const clearActiveChat", 1
        )[0]
        script = "const send = async () => {" + send + "\n" + """
(async () => {
  restaurantSyncing = true;
  input.value = '直接推薦';
  await send();
  if (calls.length || input.value !== '直接推薦' || toasts.length !== 1) {
    throw Error('typed request did not wait for restaurant sync');
  }
  restaurantSyncing = false;
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
const toasts = [];
let streamAvailable = true;
let restaurantSyncing = false;
const input = {value: '', disabled: false, focus() {}};
const getActiveThread = () => ({sessionId: 'session', decision: {revision: 'selected'}});
const appendMessage = () => {};
const showLoading = () => {};
const showToast = notice => toasts.push(notice);
const sendDecision = () => { throw Error('question incorrectly sent to decision endpoint'); };
const sendStreaming = async (...args) => {
  calls.push(['stream', ...args]);
  return streamAvailable;
};
const sendBlocking = async (...args) => { calls.push(['sync', ...args]); };
"""
        result = subprocess.run(
            ["node", "-e", setup + script], capture_output=True, text=True, encoding="utf-8"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
