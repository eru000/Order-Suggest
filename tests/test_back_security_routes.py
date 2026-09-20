import sys
import unittest
from pathlib import Path

from fastapi.routing import APIRoute

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import back  # noqa: E402
from security import require_admin_key  # noqa: E402


class ProtectedRouteTest(unittest.TestCase):
    # 會動到既有資料、或是很耗資源的操作：一定要管理金鑰。
    ADMIN_ONLY = {
        ("POST", "/api/menu/crawl"),
        ("DELETE", "/api/menu/{restaurant_name}"),
    }
    # 拍照辨識是一般使用者的主要流程，刻意不要金鑰，但一定要有限流擋濫用
    # （每次辨識都會打一次 vision API）。要重新上鎖是產品決定，不該是手滑。
    OPEN_BUT_RATE_LIMITED = {
        ("POST", "/api/upload-menu-photo"),
        ("POST", "/api/menu/vision"),
        ("POST", "/api/menu/vision/{analysis_id}/confirm"),
        ("POST", "/api/menu/vision/{analysis_id}/correct"),
    }

    def _dependencies(self):
        found = {}
        for route in back.app.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in route.methods & {"POST", "DELETE"}:
                found[(method, route.path)] = [
                    dependency.call for dependency in route.dependant.dependencies
                ]
        return found

    def test_sensitive_routes_have_admin_auth_and_rate_limit(self):
        routes = self._dependencies()
        for identity in self.ADMIN_ONLY:
            self.assertIn(identity, routes)
            dependencies = routes[identity]
            self.assertIn(require_admin_key, dependencies, identity)
            self.assertTrue(
                any(hasattr(call, "rate_limit_scope") for call in dependencies), identity
            )

    def test_photo_routes_are_open_to_users_but_always_rate_limited(self):
        routes = self._dependencies()
        for identity in self.OPEN_BUT_RATE_LIMITED:
            self.assertIn(identity, routes)
            dependencies = routes[identity]
            self.assertNotIn(require_admin_key, dependencies, identity)
            self.assertTrue(
                any(hasattr(call, "rate_limit_scope") for call in dependencies), identity
            )


if __name__ == "__main__":
    unittest.main()
