"""Mac pipe isolation and per-TV Keychain bridge regression checks."""
import io
import json
import threading
import time
import unittest
from unittest.mock import Mock

from macos.worker import Bridge, Worker, token_store


class MacBridgeTests(unittest.TestCase):
    def test_only_matching_private_pipe_reply_completes_keychain_request(self):
        output = io.StringIO()
        bridge = Bridge(output)
        results = []
        task = threading.Thread(target=lambda: results.append(bridge.request('load', 'tv:fixture')))
        task.start()
        deadline = time.monotonic() + 2
        while not output.getvalue() and time.monotonic() < deadline:
            time.sleep(.001)
        request = json.loads(output.getvalue())['value']
        bridge.reply({'reply': 'foreign', 'value': {'accesstoken': 'wrong-fixture'}})
        self.assertTrue(task.is_alive())
        bridge.reply({'reply': request['id'], 'value': {'accesstoken': 'own-fixture'}})
        task.join(2)
        self.assertFalse(task.is_alive())
        self.assertEqual(results, [{'accesstoken': 'own-fixture'}])
        self.assertEqual(bridge.pending, {})

    def test_keychain_error_does_not_return_a_token(self):
        output = io.StringIO()
        bridge = Bridge(output)
        results = []
        def request():
            try:
                bridge.request('load', 'tv:fixture')
            except RuntimeError:
                results.append('refused')
        task = threading.Thread(target=request)
        task.start()
        deadline = time.monotonic() + 2
        while not output.getvalue() and time.monotonic() < deadline:
            time.sleep(.001)
        ident = json.loads(output.getvalue())['value']['id']
        bridge.reply({'reply': ident, 'error': True})
        task.join(2)
        self.assertEqual(results, ['refused'])

    def test_token_store_uses_tv_specific_accounts(self):
        bridge = Mock()
        tokens = token_store(bridge)()
        tv = {'mac': '020000000010', 'host': '192.0.2.10'}
        tokens.load(tv)
        bridge.request.assert_called_with('load', 'tv:020000000010')
        tokens.save(tv, {'accesstoken': 'fixture'})
        bridge.request.assert_called_with('save', 'tv:020000000010', {'accesstoken': 'fixture'})

    def test_local_command_channel_cannot_inject_keychain_reply(self):
        worker = Worker.__new__(Worker)
        worker.bridge = Mock()
        worker.actions = Mock()
        worker.dispatch({'reply': 'private-request', 'value': {'accesstoken': 'fixture'}})
        worker.dispatch({'method': 'keychain', 'args': {}})
        worker.actions.submit.assert_not_called()
        worker.bridge.reply.assert_not_called()


if __name__ == '__main__':
    unittest.main()
