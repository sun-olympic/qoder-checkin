import json
import unittest
from unittest.mock import MagicMock, patch
import qoder_checkin as q

class ServerResponseLoggingTests(unittest.TestCase):
    def response(self, body, code=200):
        response = MagicMock()
        response.__enter__.return_value = response
        response.code = code
        response.headers = {}
        response.read.return_value = body
        return response

    def test_logs_unfiltered_server_campaigns_and_redacts_secrets(self):
        transport = q.Transport(q.BASES['global'], 'private-bearer')
        body = {'showCampaign': False, 'campaigns': [{'claimStatus': 'CLAIMED'}],
                'accessToken': 'secret-value', 'nested': {'appsecret': 'other-secret'},
                'message': 'private-bearer'}
        transport.opener.open = MagicMock(return_value=self.response(json.dumps(body).encode()))
        with self.assertLogs('qoder.results', level='INFO') as logs:
            result = transport.send('GET', q.CAMPAIGNS)
        entry = json.loads(logs.records[0].getMessage())
        self.assertEqual(entry['event'], 'server_response')
        self.assertEqual(entry['http_status'], 200)
        self.assertFalse(entry['body']['showCampaign'])
        self.assertEqual(entry['body']['campaigns'][0]['claimStatus'], 'CLAIMED')
        for secret in ('private-bearer', 'secret-value', 'other-secret'):
            self.assertNotIn(secret, logs.output[0])
        self.assertEqual(result.body, body)

    def test_logs_each_retry_and_non_json_failure(self):
        transport = q.Transport(q.BASES['global'], 'private-bearer')
        transport.opener.open = MagicMock(side_effect=[
            self.response(b'{"error":"busy"}', 503),
            self.response(b'<html>unavailable</html>', 200)])
        with patch.object(q.time, 'sleep'), self.assertLogs('qoder.results', level='INFO') as logs:
            with self.assertRaises(q.ApiError):
                transport.send('GET', q.CAMPAIGNS)
        entries = [json.loads(r.getMessage()) for r in logs.records]
        self.assertEqual([e['http_status'] for e in entries], [503, 200])
        self.assertEqual([e['attempt'] for e in entries], [1, 2])
        self.assertEqual(entries[1]['body'], '<html>unavailable</html>')
