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
    def test_sensitive_routes_have_admin_auth_and_rate_limit(self):
        protected = {
            ("POST", "/api/menu/crawl"),
            ("POST", "/api/upload-menu-photo"),
            ("POST", "/api/menu/vision"),
            ("POST", "/api/menu/vision/{analysis_id}/confirm"),
            ("POST", "/api/menu/vision/{analysis_id}/correct"),
            ("DELETE", "/api/menu/{restaurant_name}"),
        }
        found = set()

        for route in back.app.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in route.methods & {"POST", "DELETE"}:
                identity = (method, route.path)
                if identity not in protected:
                    continue
                found.add(identity)
                dependencies = [dependency.call for dependency in route.dependant.dependencies]
                self.assertIn(require_admin_key, dependencies, identity)
                self.assertTrue(
                    any(hasattr(call, "rate_limit_scope") for call in dependencies),
                    identity,
                )

        self.assertEqual(protected, found)


if __name__ == "__main__":
    unittest.main()
