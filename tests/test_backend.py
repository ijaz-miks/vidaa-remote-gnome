"""Regression checks for TV-specific discovery and control safety boundaries."""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import Settings, DirectController, PhoneController, parse_description, credentials

class BackendTests(unittest.TestCase):
    def settings(self):
        return SimpleNamespace(data={'uuid': '02:11:22:33:44:55',
            'tv': {'host': '192.0.2.10', 'name': 'Smart TV', 'mac': '020000000010', 'protocol': 3290}},
            set_tv=Mock())

    def direct(self):
        c = DirectController(self.settings(), Mock())
        c.client = Mock()
        c.client.is_connected.return_value = True
        c.client.publish.return_value = SimpleNamespace(rc=0)
        c.cid = '02:11:22:33:44:55$his$HASH_vidaacommon_001'
        return c

    def test_discovery_keeps_tv_advertised_port_and_wifi_identity(self):
        xml = b'<root><device><friendlyName>Smart TV</friendlyName><modelDescription>#CAP#\nmac=aaaaaaaaaaaa\nmacWifi=020000000010\ntransport_protocol=3290\nbrand=his\nvidaa_support=1</modelDescription></device></root>'
        tv = parse_description(xml, 'http://192.0.2.10:18400/MediaServer/rendererdevicedesc.xml')
        self.assertEqual(tv['description_url'], 'http://192.0.2.10:18400/MediaServer/rendererdevicedesc.xml')
        self.assertEqual(tv['mac'], '020000000010')
        self.assertEqual(tv['protocol'], 3290)

    def test_broker_connection_alone_does_not_allow_remote_commands(self):
        c = self.direct()
        with self.assertRaises(PermissionError):
            c.send_key('KEY_VOLUMEUP')
        c.client.publish.assert_not_called()

    def test_rejected_control_subscriptions_do_not_enable_controls(self):
        c = self.direct()
        c.token = {'accesstoken': 'fixture'}
        c.pending[7] = [f'/remoteapp/mobile/{c.cid}/ui_service/data/sourcelist']
        c._subscribed(c.client, None, 7, [SimpleNamespace(is_failure=True)], None)
        self.assertFalse(c.ready)

    def test_pairing_accepts_leading_zero_pin_as_integer(self):
        c = self.direct()
        c.submit_pin('0123')
        topic, payload = c.client.publish.call_args.args
        self.assertTrue(topic.endswith('/actions/authenticationcode'))
        self.assertEqual(json.loads(payload), {'authNum': 123})
        c.client.publish.reset_mock()
        with self.assertRaises(ValueError):
            c.submit_pin('123a')
        c.client.publish.assert_not_called()

    def test_tv_source_id_is_preserved_as_a_string(self):
        c = self.direct(); c.ready = True
        c.set_source({'sourceid': 'HDMI4', 'sourcename': 'HDMI4'})
        self.assertEqual(json.loads(c.client.publish.call_args.args[1]), {'sourceid': 'HDMI4'})

    def test_delayed_pin_rejection_does_not_interrupt_authorized_controls(self):
        c = self.direct(); c.ready = True
        c.token = {'accesstoken': 'own-fixture'}
        msg = SimpleNamespace(topic=f'/remoteapp/mobile/{c.cid}/ui_service/data/authenticationcode',
                              payload=b'{"result":100}')
        c._message(c.client, None, msg)
        c.emit.assert_not_called()
        self.assertTrue(c.ready)
        c.send_key('KEY_VOLUMEUP')
        c.client.publish.assert_called_once()

    def test_pin_rejection_is_reported_before_authorization(self):
        c = self.direct()
        msg = SimpleNamespace(topic=f'/remoteapp/mobile/{c.cid}/ui_service/data/authenticationcode',
                              payload=b'{"result":100}')
        c._message(c.client, None, msg)
        self.assertTrue(any(call.args[0] == 'error' for call in c.emit.call_args_list))
        self.assertFalse(c.ready)

    def test_foreign_client_token_cannot_be_saved_by_this_app(self):
        c = self.direct(); c.tokens = Mock()
        msg = SimpleNamespace(topic='/remoteapp/mobile/PHONE/platform_service/data/tokenissuance',
                              payload=b'{"accesstoken":"fixture"}')
        c._message(c.client, None, msg)
        c.tokens.save.assert_not_called()
        self.assertIsNone(c.token)

    def test_broadcast_token_cannot_authorize_the_desktop(self):
        c = self.direct(); c.tokens = Mock()
        msg = SimpleNamespace(topic='/remoteapp/mobile/broadcast/platform_service/data/tokenissuance',
                              payload=b'{"accesstoken":"fixture"}')
        c._message(c.client, None, msg)
        c.tokens.save.assert_not_called()
        self.assertIsNone(c.token)

    def test_stale_client_cannot_change_pairing_or_state(self):
        c = self.direct(); c.tokens = Mock()
        msg = SimpleNamespace(topic=f'/remoteapp/mobile/{c.cid}/platform_service/data/tokenissuance',
                              payload=b'{"accesstoken":"fixture"}')
        c._message(Mock(), None, msg)
        c.tokens.save.assert_not_called()
        self.assertIsNone(c.token)

    def test_phone_mode_never_taps_another_application(self):
        c = PhoneController(self.settings(), Mock())
        c.serial = 'fixture'; c.ready = True
        c._adb = Mock(return_value='mCurrentFocus=Window{u0 com.example.app/.Main}')
        c._tap = Mock()
        with self.assertRaises(ConnectionError):
            c.send_key('KEY_VOLUMEUP')
        c._tap.assert_not_called()
        self.assertFalse(c.ready)
        c.close()

if __name__ == '__main__':
    unittest.main()
