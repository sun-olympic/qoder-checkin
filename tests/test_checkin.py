"""Behavior tests with a local HTTP server; no real account credentials required."""
from contextlib import redirect_stdout
from datetime import datetime, time as clock_time, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import IncompleteRead
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import qoder_checkin as q


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def respond(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.seen.append((self.command, self.path, dict(self.headers), body))
        if not self.server.replies:
            status, value, headers = 500, {"error": "unexpected request"}, {}
        else:
            status, value, headers = self.server.replies.pop(0)
        encoded = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(encoded)))
        for key, val in headers.items():
            self.send_header(key, val)
        self.end_headers()
        self.wfile.write(encoded)


class HttpBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.server.replies, self.server.seen = [], []
        self.token = "test-secret-never-print"
        self.transport = q.Transport(f"http://127.0.0.1:{self.server.server_port}", self.token, 2)
        self.client = q.Client(self.transport)
        self.sleep_patch = patch.object(q.time, "sleep")
        self.sleep = self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def reply(self, value, status=200, **headers):
        self.server.replies.append((status, value, headers))

    def methods(self):
        return [r[0] for r in self.server.seen]

    def test_claim_sends_real_http_method_auth_and_body(self):
        self.reply({"status": "CLAIMABLE", "rewardCredits": 100})
        self.reply({"success": True, "rewardCredits": 100, "token": self.token})
        result = self.client.claim()
        self.assertEqual(result["result"], "claimed")
        self.assertEqual(result["rewardCredits"], 100)
        self.assertNotIn(self.token, json.dumps(result))
        self.assertEqual(self.methods(), ["GET", "POST"])
        method, path, headers, body = self.server.seen[1]
        self.assertEqual(path, q.PREFIX + "/claim")
        self.assertEqual(headers["Authorization"], "Bearer " + self.token)
        self.assertEqual(json.loads(body), {})

    def test_already_claimed_does_not_compare_to_local_midnight(self):
        # A server window crosses midnight; an old lastClaimedAt must not cause a POST.
        self.reply({"status": "CLAIMED", "lastClaimedAt": 1})
        self.assertEqual(self.client.claim()["result"], "already_claimed")
        self.assertEqual(self.methods(), ["GET"])

    def test_status_command_never_claims(self):
        self.reply({"data": {"status": "CLAIMABLE"}, "code": 0})
        self.assertEqual(self.client.status()["result"], "claimable")
        self.assertEqual(self.methods(), ["GET"])

    def test_unknown_or_unavailable_state_never_posts(self):
        self.reply({"status": "ENDED"})
        self.assertEqual(self.client.claim()["result"], "unavailable")
        self.reply({"status": "UNEXPECTED"})
        with self.assertRaises(q.ApiError):
            self.client.claim()
        self.assertEqual(self.methods(), ["GET", "GET"])

    def test_duplicate_race_is_success(self):
        for body in ({"result": "ALREADY_CLAIMED"}, {"errorCode": "AlreadyExists"}):
            self.reply({"status": "CLAIMABLE"})
            self.reply(body, 409)
            self.assertEqual(self.client.claim()["result"], "already_claimed")

    def test_unrelated_409_does_not_become_success(self):
        self.reply({"status": "CLAIMABLE"})
        self.reply({"errorCode": "OtherConflict"}, 409)
        self.reply({"status": "CLAIMABLE"})
        with self.assertRaises(q.ApiError):
            self.client.claim()
        self.assertEqual(self.methods(), ["GET", "POST", "GET"])

    def test_http_200_business_failure_and_string_false_are_not_success(self):
        for body in ({"success": False}, {"success": "false"},
                     {"code": "Forbidden", "success": True}, {"code": False, "success": True},
                     {"data": {"success": True}, "success": False}, {}):
            self.reply({"status": "CLAIMABLE"})
            self.reply(body)
            self.reply({"status": "CLAIMABLE"})
            with self.assertRaises(q.ApiError):
                self.client.claim()

    def test_get_retries_transient_failures(self):
        self.reply(b"gateway busy", 503)
        self.reply({"message": "slow down"}, 429, **{"Retry-After": "5"})
        self.reply({"status": "CLAIMABLE"})
        self.assertEqual(self.client.status()["result"], "claimable")
        self.assertEqual(self.methods(), ["GET", "GET", "GET"])
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1, 5])

    def test_post_503_is_reconciled_without_resubmitting(self):
        self.reply({"status": "CLAIMABLE"})
        self.reply(b"upstream unavailable", 503)
        self.reply({"status": "CLAIMED"})
        self.assertTrue(self.client.claim()["verified_after_claim"])
        self.assertEqual(self.methods(), ["GET", "POST", "GET"])

    def test_auth_error_is_not_retried_and_body_not_leaked(self):
        for status in (401, 403):
            self.reply({"message": self.token}, status)
            with self.assertRaises(q.AuthError) as caught:
                self.client.status()
            self.assertNotIn(self.token, str(caught.exception))
        self.assertEqual(self.methods(), ["GET", "GET"])

    def test_redirect_does_not_forward_credentials(self):
        self.reply({}, 302, Location=f"http://127.0.0.1:{self.server.server_port}/stolen")
        with self.assertRaises(q.ApiError):
            self.client.status()
        self.assertEqual(len(self.server.seen), 1)

    def test_non_json_and_invalid_shapes_fail_closed(self):
        for body in (b"<html>login</html>", [], {"data": []}, {"status": []}):
            self.reply(body)
            with self.assertRaises(q.ApiError):
                self.client.claim()
        self.assertNotIn("POST", self.methods())


class LocalBehaviorTests(unittest.TestCase):
    def test_post_timeout_reconciles_without_retry(self):
        transport = unittest.mock.Mock()
        transport.send.side_effect = [q.Response(200, {"status": "CLAIMABLE"}),
                                      q.ApiError("timeout"), q.Response(200, {"status": "CLAIMED"})]
        result = q.Client(transport).claim()
        self.assertTrue(result["verified_after_claim"])
        self.assertEqual([c.args[0] for c in transport.send.call_args_list], ["GET", "POST", "GET"])

    def test_truncated_post_response_is_not_retried(self):
        transport = q.Transport(q.BASES["cn"], "test-only")
        with patch.object(transport.opener, "open", side_effect=IncompleteRead(b"partial")) as send:
            with self.assertRaises(q.ApiError):
                transport.send("POST", q.PREFIX + "/claim")
        self.assertEqual(send.call_count, 1)

    def test_malformed_config_reports_actionable_error(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            for rows in ([{"region": []}], [None], [{"region": "cn", "name": []}]):
                path.write_text(json.dumps({"accounts": rows}))
                with self.assertRaises(q.ConfigError):
                    q.load_accounts(path, "both")

    def test_schedule_uses_beijing_time_and_runs_after_opening(self):
        at = clock_time(10, 5)
        before = datetime(2026, 9, 20, 2, 4, tzinfo=timezone.utc)
        self.assertEqual(q.next_run(before, at).isoformat(), "2026-09-20T10:05:00+08:00")
        after = datetime(2026, 9, 20, 2, 5, tzinfo=timezone.utc)
        self.assertEqual(q.next_run(after, at).isoformat(), "2026-09-21T10:05:00+08:00")

    def test_config_region_filter_and_rejects_token_exfiltration_hosts(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            rows = q.default_accounts()
            path.write_text(json.dumps({"accounts": rows}))
            self.assertEqual(len(q.load_accounts(path, "both")), 2)
            self.assertEqual(q.load_accounts(path, "cn")[0].region, "cn")
            for base in ("https://attacker.invalid", "https://gateway.qoder.com.cn.attacker.invalid",
                         "https://openapi.qoder.sh", "http://gateway.qoder.com.cn", "https://[bad"):
                rows[1]["base_url"] = base
                path.write_text(json.dumps({"accounts": rows}))
                with self.assertRaises(q.ConfigError):
                    q.load_accounts(path, "cn")

    def test_environment_overrides_file_and_credentials_are_private(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "credentials-cn.json"
            q.private_json(path, {"token": "file-secret"})
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            account = q.Account("cn", "cn", q.BASES["cn"], "QODER_TEST_TOKEN", path)
            with patch.dict(os.environ, {"QODER_TEST_TOKEN": "Bearer env-secret"}):
                self.assertEqual(account.token(), "env-secret")
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(account.token(), "file-secret")
            with self.assertRaises(FileExistsError):
                q.private_json(path, {"token": "overwritten"})

    def test_bad_token_is_not_echoed(self):
        account = q.Account("cn", "cn", q.BASES["cn"], "QODER_TEST_TOKEN")
        with patch.dict(os.environ, {"QODER_TEST_TOKEN": "secret\r\nInjected: true"}):
            with self.assertRaises(q.ConfigError) as caught:
                account.token()
        self.assertNotIn("secret", str(caught.exception))

    def test_failed_account_does_not_block_other_region(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "config.json"
            config.write_text(json.dumps({"accounts": q.default_accounts()}))
            with patch.dict(os.environ, {"QODER_CN_TOKEN": "cn-test"}, clear=True), \
                    patch.object(q.CampaignClient, "claim", return_value={"result": "claimed", "message": "ok"}):
                results = q.run_once(config, "both", "claim", 20)
            self.assertEqual([r["result"] for r in results], ["error", "claimed"])
            self.assertEqual(q.exit_code(results), 1)

    def test_json_cli_configuration_error_has_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as td, redirect_stdout(io.StringIO()) as output:
            code = q.main(["status", "--json", "--config", str(Path(td) / "absent.json")])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["accounts"][0]["result"], "error")


if __name__ == "__main__":
    unittest.main()
