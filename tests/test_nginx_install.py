"""Safe, deterministic HTTPS reverse-proxy configuration tests."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy"))
from manage_nginx import START, END, apply_snippet, remove_snippet, tls_server


HTTPS = """
server {
    listen 80 default_server;
    server_name _;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl default_server;
    listen [::]:443 ssl default_server;
    server_name _;
    ssl_certificate /etc/letsencrypt/live/partage/fullchain.pem;

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host $host;
    }
}
"""

SNIPPET = (Path(__file__).resolve().parent.parent / "deploy" /
           "nginx-https-location.conf").read_text()


class NginxTests(unittest.TestCase):
    def test_targets_https_not_http(self):
        _, pos = tls_server(HTTPS)
        amended = apply_snippet(HTTPS, SNIPPET)
        self.assertEqual(amended[:pos], HTTPS[:pos])
        self.assertIn(START, amended)
        self.assertIn(END, amended)
        self.assertIn("listen 80 default_server;", amended)
        self.assertIn("proxy_pass http://127.0.0.1:8787;", amended)
        self.assertIn("proxy_pass http://127.0.0.1:8791/;", amended)
        self.assertEqual(apply_snippet(amended, SNIPPET), amended)

    def test_ignores_fake_braces_in_nginx_comments_and_quotes(self):
        source = HTTPS.replace("    location / {",
            '    # server { listen 443; }\n'
            '    add_header X-Something "server { fake }";\n'
            "    location / {")
        amended = apply_snippet(source, SNIPPET)
        self.assertIn(START, amended)

    def test_removal_only_removes_installed_blocks(self):
        amended = apply_snippet(HTTPS, SNIPPET)
        removed = remove_snippet(amended)
        self.assertNotIn("/fbx-migration/", removed)
        self.assertIn("proxy_pass http://127.0.0.1:8787;", removed)
        self.assertIn("ssl_certificate /etc/letsencrypt/live/partage/fullchain.pem;", removed)

    def test_refuses_ambiguous_https_server_config(self):
        with self.assertRaises(ValueError):
            apply_snippet(HTTPS + HTTPS, SNIPPET)

    def test_refuses_duplicate_existing_route(self):
        with self.assertRaises(ValueError):
            apply_snippet(HTTPS.replace("    location / {",
                "    location /fbx-migration/ { }\n    location / {"), SNIPPET)


if __name__ == "__main__":
    unittest.main()
