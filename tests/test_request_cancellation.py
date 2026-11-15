import asyncio
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from starlette.requests import Request  # noqa: E402

import api_compat  # noqa: E402


def make_request(disconnected: bool) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/chat/stream",
        "headers": [],
        "query_string": b"",
    }

    async def receive():
        return {"type": "http.disconnect"} if disconnected else {"type": "http.request"}

    return Request(scope, receive)


async def cancelled_call_next(_request):
    raise asyncio.CancelledError()


class RequestCancellationTest(unittest.TestCase):
    """請求被取消有兩種原因，處理方式必須不同。

    使用者關掉分頁時原本會讓 uvicorn 印出整頁 traceback，看起來像系統壞掉；
    但伺服器關閉時的取消一定要往上拋，吞掉會拖住關閉流程。
    """

    def test_client_disconnect_is_not_an_error(self):
        response = asyncio.run(
            api_compat._request_timing(make_request(True), cancelled_call_next)
        )
        self.assertEqual(response.status_code, 499)

    def test_shutdown_cancellation_still_propagates(self):
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(
                api_compat._request_timing(make_request(False), cancelled_call_next)
            )

    def test_normal_request_is_unaffected(self):
        class FakeResponse:
            status_code = 200
            body_iterator = None

        async def ok_call_next(_request):
            return FakeResponse()

        response = asyncio.run(api_compat._request_timing(make_request(False), ok_call_next))
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
