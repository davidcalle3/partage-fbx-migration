"""Browser Basic authentication and secret-file safety regression tests."""
import base64
import os
import stat
import tempfile
import unittest
from pathlib import Path

from http_auth import BasicAuth, generate


class HttpBasicAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.filename = Path(self.temp.name) / "http-auth.json"

    def test_password_is_random_and_only_hash_saved(self):
        username, password = generate(self.filename)
        text = self.filename.read_text()
        self.assertEqual(username, "migration")
        self.assertNotIn(password, text)
        self.assertFalse(stat.S_IMODE(self.filename.stat().st_mode) & 0o077)
        auth = BasicAuth(self.filename)
        header = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        self.assertTrue(auth.verify(header))
        self.assertTrue(auth.verify(header))  # repeat requests use cached valid hash
        wrong = "Basic " + base64.b64encode(f"{username}:wrong".encode()).decode()
        self.assertFalse(auth.verify(wrong))
        self.assertFalse(auth.verify(""))
        self.assertFalse(auth.verify("Bearer invalid"))
        self.assertFalse(auth.verify("Basic @@@@"))
        self.assertFalse(auth.verify("Basic " + base64.b64encode(b"not-colon").decode()))

    def test_refuses_to_overwrite_existing_credentials(self):
        generate(self.filename)
        with self.assertRaises(FileExistsError):
            generate(self.filename)

    def test_refuses_world_readable_credential_file(self):
        generate(self.filename)
        os.chmod(self.filename, 0o644)
        with self.assertRaises(RuntimeError):
            BasicAuth(self.filename)


if __name__ == "__main__":
    unittest.main()
