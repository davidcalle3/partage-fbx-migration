"""Security contract: no HTTP migration action without valid browser auth."""
import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from starlette.responses import Response

import app
from http_auth import BasicAuth, generate


class AuthenticationMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.username, self.password = generate(Path(self.temp.name) / "auth.json")
        self.auth = BasicAuth(Path(self.temp.name) / "auth.json")
        app.runtime["auth"] = self.auth
        self.addCleanup(app.runtime.pop, "auth", None)

    async def request(self, path, header=""):
        called = []
        async def next_handler(req):
            called.append(True)
            return Response("ok", status_code=200)
        request = SimpleNamespace(url=SimpleNamespace(path=path),
                                  headers={"authorization":header} if header else {})
        return await app.security_headers(request, next_handler), called

    async def test_migration_api_requires_auth(self):
        response, called = await self.request("/api/jobs")
        self.assertEqual(response.status_code, 401)
        self.assertFalse(called)
        self.assertIn("Basic", response.headers["WWW-Authenticate"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_all_static_content_requires_auth(self):
        for path in ("/","/static/app.js","/api/report.csv","/api/queue"):
            result, called = await self.request(path)
            self.assertEqual(result.status_code, 401)
            self.assertFalse(called)

    async def test_valid_credentials_allow_request(self):
        creds = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        result, called = await self.request("/api/jobs", "Basic " + creds)
        self.assertEqual(result.status_code, 200)
        self.assertTrue(called)

    async def test_health_can_be_probed_locally(self):
        response, called = await self.request("/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(called)


if __name__ == "__main__":
    unittest.main()
