import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import checkin_cli

from qoder_auth import bearer_from_headers, capture_token_from_request, is_qoder_api_url


class BrowserAuthTests(unittest.TestCase):
    def test_wizard_uses_browser_login_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            answers = ["global", "", "", "", "1"]
            with patch("builtins.input", side_effect=answers), \
                    patch.object(checkin_cli.qoder_auth, "browser_login") as login, \
                    patch.object(checkin_cli, "save_config"):
                checkin_cli.wizard(type("Args", (), {"config": config})())
            login.assert_called_once()

    def test_accepts_case_insensitive_authorization_header(self):
        self.assertEqual(
            bearer_from_headers({"aUtHoRiZaTiOn": "Bearer abc.def"}, "global"),
            "abc.def",
        )

    def test_rejects_non_bearer_and_invalid_values(self):
        self.assertIsNone(bearer_from_headers({"Authorization": "Basic abc"}, "global"))
        self.assertIsNone(bearer_from_headers({"Authorization": "Bearer bad\nvalue"}, "global"))

    def test_only_allows_official_region_api_hosts(self):
        self.assertTrue(is_qoder_api_url("https://openapi.qoder.sh/sash/api", "global"))
        self.assertFalse(is_qoder_api_url("https://gateway.qoder.com.cn/sash/api", "global"))
        self.assertFalse(is_qoder_api_url("https://evil.example/sash/api", "global"))

    def test_captures_only_matching_region_request(self):
        self.assertEqual(
            capture_token_from_request(
                "https://gateway.qoder.com.cn/sash/api/v1/me/campaigns",
                {"Authorization": "Bearer cn-token"}, "cn"),
            "cn-token",
        )
        self.assertIsNone(capture_token_from_request(
            "https://gateway.qoder.com.cn/sash/api/v1/me/campaigns",
            {"Authorization": "Bearer cn-token"}, "global"))


if __name__ == "__main__":
    unittest.main()
