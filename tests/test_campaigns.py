import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import qoder_checkin as q
from test_checkin import Handler
from http.server import ThreadingHTTPServer
import threading


def daily(identity="daily-current", state="CLAIMABLE", start=100, end=200):
    return {"campaignId": identity, "actionType": "CLAIM_BENEFIT", "claimStatus": state,
            "startAt": start, "endAt": end, "benefit": {"kind": "CREDITS", "amount": 100},
            "placements": [{"type": "POPUP", "content": {"zh": {"title": "每天领 100 Credits"}}}]}


def listing(*rows, uid="current-user"):
    return q.Response(200, {"uid": uid, "showCampaign": bool(rows), "campaigns": list(rows)})


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.transport = Mock()
        self.client = q.CampaignClient(self.transport, "current-user")
        self.clock = patch.object(q.time, "time", return_value=150)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_new_api_claim_and_verify_same_campaign(self):
        self.transport.send.side_effect = [listing(daily()), q.Response(200, {"status": "CLAIMED"}),
                                           listing(daily(state="CLAIMED"))]
        result = self.client.claim()
        self.assertEqual(result["result"], "claimed")
        self.assertTrue(result["verified_after_claim"])
        calls = self.transport.send.call_args_list
        self.assertEqual([call.args[:2] for call in calls], [
            ("GET", q.CAMPAIGNS), ("POST", q.CAMPAIGNS + "/daily-current/claim"), ("GET", q.CAMPAIGNS)])
        self.assertEqual(calls[1].kwargs, {"empty_body": True})

    def test_repeat_run_only_reads(self):
        self.transport.send.return_value = listing(daily(state="CLAIMED"))
        self.assertEqual(self.client.claim()["result"], "already_claimed")
        self.assertEqual(self.transport.send.call_count, 1)

    def test_account_mismatch_stops_before_post(self):
        self.transport.send.return_value = listing(daily(), uid="old-user")
        with self.assertRaises(q.AuthError):
            self.client.claim()
        self.assertEqual(self.transport.send.call_count, 1)

    def test_does_not_claim_subscription_or_non_daily_model_offer(self):
        promo = daily("subscription")
        promo["actionType"] = "VIEW_DETAILS"
        model = daily("sota")
        model["benefit"]["amount"] = 4000
        model["placements"][0]["content"]["zh"]["title"] = "SOTA 模型限时体验"
        self.transport.send.return_value = listing(promo, model)
        self.assertEqual(self.client.claim()["result"], "unavailable")
        self.assertEqual(self.transport.send.call_count, 1)

    def test_new_daily_id_is_discovered(self):
        self.transport.send.return_value = listing(daily("yesterday", "CLAIMED", 1, 99),
                                                   daily("new-server-generated-id"))
        self.assertEqual(self.client.status()["campaign_id"], "new-server-generated-id")

    def test_expired_and_future_campaigns_are_not_claimed(self):
        for row in (daily(end=149), daily(start=151), daily(state="CLAIMED", end=149)):
            self.transport.send.return_value = listing(row)
            self.assertEqual(self.client.claim()["result"], "unavailable")

    def test_unknown_or_ambiguous_campaign_stops(self):
        for rows in ([daily(state="UNKNOWN")], [daily(), daily("another")], [daily("../escape")]):
            self.transport.send.return_value = listing(*rows)
            with self.assertRaises(q.ApiError):
                self.client.claim()

    def test_post_timeout_reconciles_without_second_post(self):
        self.transport.send.side_effect = [listing(daily()), q.ApiError("timeout"), listing(daily(state="CLAIMED"))]
        self.assertTrue(self.client.claim()["verified_after_claim"])
        self.assertEqual([c.args[0] for c in self.transport.send.call_args_list], ["GET", "POST", "GET"])

    def test_claimed_other_campaign_does_not_confirm_this_one(self):
        self.transport.send.side_effect = [listing(daily()), q.Response(200, {}),
                                           listing(daily(), daily("unrelated", "CLAIMED"))]
        with self.assertRaises(q.ApiError):
            self.client.claim()

    def test_explicit_failure_is_not_success(self):
        self.transport.send.side_effect = [listing(daily()), q.Response(200, {"success": False, "status": "CLAIMED"}),
                                           listing(daily())]
        with self.assertRaises(q.ApiError):
            self.client.claim()

    def test_acknowledged_claim_survives_read_failure(self):
        self.transport.send.side_effect = [listing(daily()), q.Response(200, {"data": {"status": "CLAIMED"}}),
                                           q.ApiError("read failed")]
        result = self.client.claim()
        self.assertEqual(result["result"], "claimed")
        self.assertFalse(result["verified_after_claim"])

    def test_malformed_list_is_error_not_empty_success(self):
        self.transport.send.return_value = q.Response(200, {"uid": "current-user", "campaigns": {}})
        with self.assertRaises(q.ApiError):
            self.client.claim()

    def test_macos_sync_rejects_changed_account(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "credentials-global.json"
            path.write_text(json.dumps({"token": "do-not-print", "account_id": "other-user"}))
            account = q.Account("global", "global", q.BASES["global"], "TEST_TOKEN", path,
                                auth_source="macos", expected_account_id="current-user")
            with patch.object(q.sys, "platform", "darwin"), patch.object(q.shutil, "which", return_value="node"), \
                    patch.object(q.subprocess, "run", return_value=Mock(returncode=0)), patch.dict(q.os.environ, {}, clear=True):
                with self.assertRaises(q.AuthError) as caught:
                    account.sync_login()
                self.assertNotIn("do-not-print", str(caught.exception))

    def test_real_http_new_endpoint_headers_empty_post(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.seen = []
        server.replies = [(200, listing(daily()).body, {}), (200, {"status": "CLAIMED"}, {}),
                          (200, listing(daily(state="CLAIMED")).body, {})]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            transport = q.Transport(f"http://127.0.0.1:{server.server_port}", "test-secret", 2, "0.4.2")
            result = q.CampaignClient(transport, "current-user").claim()
            self.assertTrue(result["verified_after_claim"])
            method, path, headers, body = server.seen[1]
            self.assertEqual(path, q.CAMPAIGNS + "/daily-current/claim")
            self.assertEqual(body, b"")
            self.assertEqual(headers["Cosy-Clienttype"], "10")
            self.assertEqual(headers["Cosy-Version"], "0.4.2")
            self.assertEqual(headers["Authorization"], "Bearer test-secret")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

class HiddenCampaignTests(unittest.TestCase):
    def test_hidden_claimed_daily_campaign_is_still_reported_as_claimed(self):
        transport = Mock()
        transport.send.return_value = q.Response(200, {
            'uid': 'current-user', 'showCampaign': False,
            'campaigns': [daily(state='CLAIMED')],
        })
        with patch.object(q.time, 'time', return_value=150):
            result = q.CampaignClient(transport, 'current-user').claim()
        self.assertEqual(result['result'], 'already_claimed')
        transport.send.assert_called_once_with('GET', q.CAMPAIGNS)

    def test_hidden_claimable_campaign_is_not_automatically_claimed(self):
        transport = Mock()
        transport.send.return_value = q.Response(200, {
            'uid': 'current-user', 'showCampaign': False, 'campaigns': [daily()],
        })
        with patch.object(q.time, 'time', return_value=150):
            result = q.CampaignClient(transport, 'current-user').claim()
        self.assertEqual(result['server_status'], 'NO_DAILY_CAMPAIGN')
        transport.send.assert_called_once_with('GET', q.CAMPAIGNS)

    def test_missing_campaign_notice_does_not_claim_checkin_is_unavailable(self):
        import qoder_notify as notify
        title, content = notify.result_message({
            'result': 'unavailable', 'server_status': 'NO_DAILY_CAMPAIGN', 'region': 'global',
        })
        self.assertNotIn('暂不可签到', title)
        self.assertIn('未返回', content)
        self.assertIn('不代表未签到', content)
