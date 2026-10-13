"""Local VIDAA protocol, discovery, keyring storage, and USB phone control."""
from __future__ import annotations
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import sys

ROOT = Path(__file__).resolve().parent
import paho.mqtt.client as mqtt

CONFIG_DIR = (Path.home() / 'Library/Application Support/VIDAA Remote' if sys.platform == 'darwin'
              else Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'vidaa-remote')
CONFIG_FILE = CONFIG_DIR / 'settings.json'
CERT_FILE = CONFIG_DIR / 'client.pem'
ADB = os.environ.get('VIDAA_ADB') or shutil.which('adb') or 'adb'
PACKAGE = 'com.universal.remote.multi'
APP_ID = 'dev.local.VidaaRemote'
POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix='vidaa')


def credentials(uuid, brand='his', protocol=3290, timestamp=None):
    t = int(time.time()) if timestamp is None else int(timestamp)
    md5 = lambda s: hashlib.md5(s.encode()).hexdigest().upper()
    cid = f'{uuid}${brand}${md5("38D65DC30F45109A369A86FCE866A85B$" + uuid)[:6]}_vidaacommon_001'
    user = f'{brand}${t if protocol < 3000 else t ^ 0x569814772b03a968}'
    suffix = 'h!i@s#$v%i^d&a*a' if protocol >= 3290 else 'h*i&s%e!r^v0i1c9'
    value = md5(f'{brand}{sum(map(int, str(t))) % 10}{suffix}')[:6]
    return cid, user, md5(f'{t}${value}')


def token_expiry(token, kind, default_days):
    """TV firmware can encode token timestamps as decimal strings."""
    try:
        started = int(token.get(kind + '_time', token.get('saved_at', 0)))
        days = int(token.get(kind + '_duration_day', default_days))
    except (TypeError, ValueError, OverflowError):
        return 0
    return started + days * 86400 if started > 0 and days >= 0 else 0


def read_description(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'http' or parsed.username or parsed.password or not parsed.hostname:
        raise ValueError('The TV returned an invalid discovery address.')
    with urllib.request.urlopen(url, timeout=3) as response:
        data = response.read(131073)
    if len(data) > 131072:
        raise ValueError('The TV description is too large.')
    return parse_description(data, url)


def parse_description(data, url):
    root = ET.fromstring(data)
    values = {e.tag.split('}')[-1]: e.text or '' for e in root.iter()}
    cap = {}
    for line in values.get('modelDescription', '').splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            cap[k.strip()] = v.strip()
    if cap.get('vidaa_support') != '1' and cap.get('brand') not in ('his', 'tos'):
        raise ValueError('This device does not advertise VIDAA support.')
    try:
        protocol = int(cap.get('transport_protocol', '3290'))
    except ValueError:
        protocol = 3290
    host = urllib.parse.urlsplit(url).hostname
    mac = cap.get('macWifi') or cap.get('mac') or cap.get('macEthernet', '')
    return {'host': host, 'name': values.get('friendlyName') or 'VIDAA TV',
            'description_url': url, 'protocol': protocol, 'brand': cap.get('brand', 'his'),
            'mac': re.sub('[^0-9a-f]', '', mac.lower()), 'model': '',
            'caps': cap, 'cert_sha256': ''}


def manual_device(host):
    host = host.strip()
    if not re.fullmatch(r'[A-Za-z0-9.:_-]{1,253}', host):
        raise ValueError('Enter a TV IP address or hostname.')
    display_host = f'[{host}]' if ':' in host else host
    for port in (18400, 38400):
        try:
            return read_description(f'http://{display_host}:{port}/MediaServer/rendererdevicedesc.xml')
        except (OSError, ValueError, ET.ParseError):
            pass
    raise ConnectionError('The TV did not answer. Check that it is on and connected to this network.')


def discover(timeout=3.5):
    locations = set()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.settimeout(.3)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        query = ('M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n'
                 'MAN: "ssdp:discover"\r\nMX: 2\r\n'
                 'ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n')
        sock.sendto(query.encode(), ('239.255.255.250', 1900))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                body, address = sock.recvfrom(16384)
            except socket.timeout:
                continue
            headers = {}
            for line in body.decode(errors='replace').splitlines():
                if ':' in line:
                    k, v = line.split(':', 1)
                    headers[k.lower()] = v.strip()
            url = headers.get('location', '')
            if urllib.parse.urlsplit(url).hostname == address[0]:
                locations.add(url)
    devices = {}
    for url in sorted(locations)[:32]:
        try:
            tv = read_description(url)
            devices[tv['mac'] or tv['host']] = tv
        except (OSError, ValueError, ET.ParseError):
            continue
    return list(devices.values())


class Settings:
    def __init__(self):
        self.lock = threading.RLock()
        CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        if CONFIG_FILE.exists():
            self.data = json.loads(CONFIG_FILE.read_text())
        else:
            uuid = ':'.join(f'{b:02x}' for b in bytes([2]) + secrets.token_bytes(5))
            self.data = {'uuid': uuid, 'mode': 'direct', 'tv': {}, 'window': [720, 850]}
            self.save()

    def save(self):
        with self.lock:
            fd = os.open(CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(self.data, f, indent=2)
                f.write('\n')

    def set_tv(self, tv):
        with self.lock:
            prior = self.data.get('tv', {})
            if prior.get('mac') == tv.get('mac') and prior.get('mac'):
                tv = {**prior, **{k: v for k, v in tv.items() if v not in ('', None)}}
            self.data['tv'] = tv
            self.save()
        return dict(tv)


class Tokens:
    """Store only this application's own pairing in GNOME Keyring."""
    def __init__(self):
        import gi
        gi.require_version('Secret', '1')
        from gi.repository import Secret
        self.secret = Secret
        self.schema = Secret.Schema.new(APP_ID, Secret.SchemaFlags.NONE,
                                         {'tv': Secret.SchemaAttributeType.STRING})

    def load(self, tv):
        raw = self.secret.password_lookup_sync(self.schema, {'tv': tv['mac'] or tv['host']}, None)
        if not raw:
            return None
        return json.loads(raw)

    def save(self, tv, value):
        return self.secret.password_store_sync(self.schema, {'tv': tv['mac'] or tv['host']},
                                               self.secret.COLLECTION_DEFAULT,
                                               'VIDAA Remote: ' + tv['name'], json.dumps(value), None)


class PinnedMQTT(mqtt.Client):
    def __init__(self, *args, fingerprint='', on_fingerprint=None, **kwargs):
        self.expected_fingerprint = fingerprint
        self.fingerprint_callback = on_fingerprint
        super().__init__(*args, **kwargs)

    def _ssl_wrap_socket(self, tcp_sock):
        sock = super()._ssl_wrap_socket(tcp_sock)
        fingerprint = hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()
        if self.expected_fingerprint and fingerprint != self.expected_fingerprint:
            sock.close()
            raise ssl.SSLError('The TV certificate changed. Verify the TV before connecting again.')
        if self.fingerprint_callback:
            self.fingerprint_callback(fingerprint)
        return sock


class DirectController:
    mode = 'direct'
    def __init__(self, settings, emit):
        self.settings, self.emit = settings, emit
        self.tv = dict(settings.data.get('tv', {}))
        self.client = None
        self.ready = False
        self.closed = False
        self.token = None
        self.pending = {}
        self.refreshing = False
        self.pairing_confirmed = False
        self.reconnecting_token = False
        self.cid = self.username = ''
        self.supported_keys = None

    def connect(self):
        if self.closed:
            return
        if not CERT_FILE.is_file():
            raise ConnectionError('Import the VIDAA client certificate before direct pairing. See Certificate setup in the README.')
        self.emit('status', {'state': 'connecting', 'text': 'Connecting to TV…'})
        self.tokens = Tokens()
        token = self.tokens.load(self.tv) or self.token
        self.token = None
        self.refreshing = False
        now = time.time()
        if token and token.get('uuid') == self.settings.data['uuid']:
            expires = token_expiry(token, 'accesstoken', 7)
            if token.get('accesstoken') and now < expires:
                self.token = token
            elif token.get('refreshtoken') and now < token_expiry(token, 'refreshtoken', 30):
                self.token = token
                self.refreshing = True
        if self.token:
            self.cid, self.username = token['client_id'], token['username']
            password = token['refreshtoken'] if self.refreshing else token['accesstoken']
        else:
            self.cid, self.username, password = credentials(self.settings.data['uuid'],
                  self.tv.get('brand', 'his'), self.tv.get('protocol', 3290))
        def pin(fingerprint):
            self.tv['cert_sha256'] = fingerprint
            self.settings.set_tv(self.tv)
        client = PinnedMQTT(mqtt.CallbackAPIVersion.VERSION2, client_id=self.cid,
                 protocol=mqtt.MQTTv311, reconnect_on_failure=False,
                 fingerprint=self.tv.get('cert_sha256', ''), on_fingerprint=pin)
        self.client = client
        self.pending.clear()
        client.username_pw_set(self.username, password)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(str(CERT_FILE))
        client.tls_set_context(ctx)
        client.on_connect = self._connect
        client.on_disconnect = self._disconnect
        client.on_message = self._message
        client.on_subscribe = self._subscribed
        client.connect(self.tv['host'], 36669, keepalive=30)
        if self.closed:
            client.disconnect()
            return
        client.loop_start()

    def _active(self, client):
        return not self.closed and client is self.client

    def _connect(self, client, userdata, flags, reason, properties):
        if not self._active(client):
            return
        if reason.is_failure:
            self.emit('status', {'state': 'error', 'text': 'The TV refused the connection.'})
            return
        self.emit('status', {'state': 'pairing', 'text': 'Connected. Pair this desktop with your TV.'})
        self._subscribe()
        if self.refreshing:
            self.client.publish(f'/remoteapp/tv/platform_service/{self.cid}/data/gettoken',
                                json.dumps({'refreshtoken': self.token['refreshtoken']}), qos=0)

    def _subscribe(self):
        base = f'/remoteapp/mobile/{self.cid}'
        pairs = [f'{base}/ui_service/data/{x}' for x in
                 ('authentication', 'authenticationcode', 'authenticationcodetoast',
                  'authenticationcodeclose', 'tokenissuance', 'vidaa_app_connect')]
        pairs += [f'{base}/platform_service/data/tokenissuance']
        data = [f'{base}/platform_service/data/{x}' for x in ('getdeviceinfo', 'gettvinfo')]
        data += [f'{base}/ui_service/data/{x}' for x in ('capability', 'sourcelist', 'applist', 'state')]
        data += ['/remoteapp/mobile/broadcast/ui_service/state',
                 '/remoteapp/mobile/broadcast/ui_service/volume',
                 '/remoteapp/mobile/broadcast/platform_service/actions/volumechange',
                 '/remoteapp/mobile/broadcast/platform_service/actions/tvsleep']
        rc, mid = self.client.subscribe([(topic, 1) for topic in pairs + data])
        self.pending[mid] = pairs + data

    def _subscribed(self, client, userdata, mid, reasons, properties):
        if not self._active(client):
            return
        names = self.pending.pop(mid, [])
        allowed = {name for name, reason in zip(names, reasons) if not reason.is_failure}
        if self.token and not self.refreshing and any(x.endswith('/sourcelist') for x in allowed):
            self.ready = True
            self.emit('status', {'state': 'ready', 'text': 'Connected directly to TV'})
            self.refresh()
        elif self.tv.get('protocol', 3290) < 3000 and any(x.endswith('/sourcelist') for x in allowed):
            self.ready = True
            self.emit('status', {'state': 'ready', 'text': 'Connected directly to TV'})
            self.refresh()
        else:
            self.emit('status', {'state': 'pairing', 'text': 'Pair this desktop to enable controls.'})

    def _disconnect(self, client, userdata, flags, reason, properties):
        self.ready = False
        if self._active(client):
            self.emit('status', {'state': 'offline', 'text': 'TV connection interrupted. Reconnect to continue.'})

    def _publish(self, service, action, payload='', *, command=False):
        if self.closed or not self.client or not self.client.is_connected():
            raise ConnectionError('Connect to the TV first.')
        if command and not self.ready:
            raise PermissionError('Pair this desktop with the TV first.')
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        result = self.client.publish(f'/remoteapp/tv/{service}/{self.cid}/actions/{action}', payload, qos=0, retain=False)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionError('The command could not be sent.')

    def pair(self):
        self.pairing_confirmed = False
        self._publish('ui_service', 'vidaa_app_connect',
                      {'app_version': 2, 'connect_result': 0, 'device_type': 'Mobile App'})
        self._publish('ui_service', 'gettvstate')
        self.emit('pair-requested', {})

    def submit_pin(self, pin):
        if not re.fullmatch(r'\d{4}', pin):
            raise ValueError('Enter the four-digit code shown on the TV.')
        self._publish('ui_service', 'authenticationcode', {'authNum': int(pin)})

    def _request_token(self):
        self.client.publish(f'/remoteapp/tv/platform_service/{self.cid}/data/gettoken',
                            json.dumps({'refreshtoken': ''}), qos=0)
        self._publish('ui_service', 'authenticationcodeclose')

    def _resume_with_token(self, previous_client):
        """Apply the newly issued password on a fresh MQTT connection."""
        try:
            if not self._active(previous_client):
                return
            self.ready = False
            self.client = None
            previous_client.disconnect()
            previous_client.loop_stop()
            if not self.closed:
                self.connect()
        except Exception:
            if not self.closed:
                self.emit('status', {'state': 'error', 'text': 'Pairing received. Reconnect to apply the TV authorization.'})
        finally:
            self.reconnecting_token = False

    def _message(self, client, userdata, message):
        if not self._active(client):
            return
        try:
            if len(message.payload) > 2097152:
                return
            data = json.loads(message.payload) if message.payload else {}
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except ValueError:
                    data = {'message': data}
            topic = message.topic
            prefix = f'/remoteapp/mobile/{self.cid}/'
            if not topic.startswith(prefix) and not topic.startswith('/remoteapp/mobile/broadcast/'):
                return
            token_topics = {prefix + service + '/data/tokenissuance' for service in
                            ('ui_service', 'platform_service')}
            if 'tokenissuance' in topic and topic not in token_topics:
                return
            if topic in token_topics and isinstance(data, dict) and data.get('accesstoken'):
                prior_refresh = (self.token or {}).get('refreshtoken', '')
                self.refreshing = False
                self.token = {'refreshtoken': prior_refresh, **data, 'client_id': self.cid, 'username': self.username,
                              'uuid': self.settings.data['uuid'], 'saved_at': int(time.time())}
                # Only a token issued to this application's own desktop identity is stored.
                saved = self.tokens.save(self.tv, self.token)
                if not saved:
                    self.emit('error', 'Paired for this session, but GNOME Keyring could not save the pairing.')
                try:
                    self._publish('ui_service', 'authenticationcodeclose')
                except ConnectionError:
                    pass
                self.emit('paired', {'saved': bool(saved)})
                if not self.reconnecting_token:
                    self.reconnecting_token = True
                    POOL.submit(self._resume_with_token, client)
            elif topic == prefix + 'ui_service/data/vidaa_app_connect':
                if isinstance(data, dict) and data.get('connect_result') == 1:
                    self.pairing_confirmed = True
                    self.emit('pair-acknowledged', {})
            elif 'authentication' in topic:
                if topic.endswith('/authenticationcode'):
                    result = data.get('result') if isinstance(data, dict) else None
                    self.emit('pairing-response', {'result': result,
                              'fields': list(data) if isinstance(data, dict) else type(data).__name__})
                    if str(result) == '1':
                        self._request_token()
                    else:
                        self.emit('error', 'The TV did not accept the pairing code. Request a new code and try again.')
                elif isinstance(data, dict) and data.get('result') == 1:
                    self._request_token()
                elif isinstance(data, dict) and data.get('result') == 0:
                    self.emit('error', 'The TV did not accept that pairing code.')
                elif not topic.endswith('authenticationcodeclose'):
                    self.pairing_confirmed = True
                    self.emit('pin-needed', {})
            elif 'volume' in topic and isinstance(data, dict):
                if data.get('volume_type') == 0:
                    value = data.get('volume_value', data.get('volume', data.get('value')))
                    if value is not None:
                        self.emit('volume', max(0, min(100, int(value))))
                elif data.get('volume_type') == 2:
                    self.emit('muted', bool(data.get('volume_value')))
            elif topic.endswith('/sourcelist') and isinstance(data, list):
                self.emit('sources', data)
            elif topic.endswith('/applist') and isinstance(data, list):
                self.emit('apps', data)
            elif topic.endswith('/getdeviceinfo') and isinstance(data, dict):
                self.tv.update({'model': data.get('model_name', ''), 'firmware': data.get('tv_version', ''),
                                'name': data.get('tv_name') or self.tv['name']})
                self.settings.set_tv(self.tv)
                self.emit('device', dict(self.tv))
            elif topic.endswith('/gettvinfo') and isinstance(data, dict):
                self.emit('capabilities', data)
            elif topic.endswith('/state'):
                self.emit('tv-state', data)
        except (ValueError, TypeError, KeyError):
            self.emit('error', 'The TV sent an unreadable response.')
        except Exception:
            self.emit('error', 'The TV pairing could not be saved to GNOME Keyring.')

    def refresh(self):
        for service, action in [('platform_service', 'getdeviceinfo'), ('platform_service', 'gettvinfo'),
               ('platform_service', 'getvolume'), ('ui_service', 'gettvstate'),
               ('ui_service', 'sourcelist'), ('ui_service', 'applist')]:
            self._publish(service, action)

    def send_key(self, key):
        if not re.fullmatch(r'KEY_[A-Z0-9_]+', key):
            raise ValueError('Unknown remote key.')
        self._publish('remote_service', 'sendkey', key, command=True)

    def set_volume(self, volume):
        self._publish('platform_service', 'changevolume', str(max(0, min(100, int(volume)))), command=True)

    def set_source(self, source):
        self._publish('ui_service', 'changesource', {'sourceid': str(source['sourceid'])}, command=True)

    def launch_app(self, app):
        self._publish('ui_service', 'launchapp', app, command=True)

    def close(self):
        self.closed = True
        self.ready = False
        if self.client:
            self.client.disconnect()
            self.client.loop_stop()


def dlna_volume(tv):
    service = 'urn:schemas-upnp-org:service:RenderingControl:1'
    parsed = urllib.parse.urlsplit(tv['description_url'])
    endpoint = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, '/upnp/control/mingusrcr', '', ''))
    body = (f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            f'<u:GetVolume xmlns:u="{service}"><InstanceID>0</InstanceID>'
            '<Channel>Master</Channel></u:GetVolume></s:Body></s:Envelope>')
    request = urllib.request.Request(endpoint, data=body.encode(),
             headers={'Content-Type': 'text/xml', 'SOAPAction': f'"{service}#GetVolume"'})
    with urllib.request.urlopen(request, timeout=3) as response:
        root = ET.fromstring(response.read(16384))
    value = int(next(e.text for e in root.iter() if e.tag.split('}')[-1] == 'CurrentVolume'))
    return max(0, min(100, value))


class PhoneController:
    """Use the user's installed, paired VIDAA app without exporting its credentials."""
    mode = 'phone'
    KEY_IDS = {'KEY_UP': 'linear_menu_top', 'KEY_DOWN': 'linear_menu_bottom',
               'KEY_LEFT': 'linear_menu_left', 'KEY_RIGHT': 'linear_menu_right',
               'KEY_OK': 'linear_menu_ok', 'KEY_RETURNS': 'linear_menu_return',
               'KEY_MENU': 'linear_menu_menu', 'KEY_HOME': 'linear_menu_home',
               'KEY_MUTE': 'linear_menu_mute'}
    supported_keys = set(KEY_IDS) | {'KEY_VOLUMEUP', 'KEY_VOLUMEDOWN', 'KEY_CHANNELUP', 'KEY_CHANNELDOWN'}
    def __init__(self, settings, emit):
        self.settings, self.emit = settings, emit
        self.tv = dict(settings.data.get('tv', {}))
        self.serial = ''
        self.ready = False
        self.closed = False
        self.tree = None
        self.actions = ThreadPoolExecutor(max_workers=1, thread_name_prefix='vidaa-phone')

    def _adb(self, *args, timeout=12):
        result = subprocess.run([ADB, '-s', self.serial, *args], capture_output=True,
                                text=True, timeout=timeout)
        if result.returncode:
            raise ConnectionError('The USB phone is unavailable. Reconnect it and try again.')
        return result.stdout

    def _tree(self):
        result = self._adb('exec-out', 'uiautomator', 'dump', '/dev/tty')
        try:
            self.tree = ET.fromstring(result[result.index('<?xml'):result.index('</hierarchy>') + 12])
        except (ValueError, ET.ParseError):
            raise ConnectionError('VIDAA is not ready on the phone. Reconnect to open its remote.')
        return self.tree

    @staticmethod
    def _center(node):
        numbers = [int(n) for n in re.findall(r'\d+', node.get('bounds', ''))]
        if len(numbers) != 4:
            raise ValueError('The phone control has no usable position.')
        x1, y1, x2, y2 = numbers
        return str((x1 + x2) // 2), str((y1 + y2) // 2)

    def _node(self, name, root=None):
        for node in (self.tree if root is None else root).iter('node'):
            if node.get('package') == PACKAGE and node.get('resource-id', '').endswith('/' + name):
                return node
        raise ConnectionError('This phone remote layout is unsupported. Reconnect to refresh its controls.')

    def _tap(self, node):
        x, y = self._center(node)
        self._adb('shell', 'input', 'swipe', x, y, x, y, '80')

    def connect(self):
        self.emit('status', {'state': 'connecting', 'text': 'Opening VIDAA on your USB phone…'})
        result = subprocess.run([ADB, 'devices'], capture_output=True, text=True, timeout=8)
        devices = [line.split()[0] for line in result.stdout.splitlines()[1:]
                   if len(line.split()) >= 2 and line.split()[1] == 'device']
        preferred = os.environ.get('VIDAA_DEVICE_SERIAL', '')
        if preferred:
            if preferred not in devices:
                raise ConnectionError('The selected Android phone is unavailable over USB.')
            self.serial = preferred
        elif len(devices) == 1:
            self.serial = devices[0]
        else:
            raise ConnectionError('Connect one Android phone with USB debugging authorized, or select it with VIDAA_DEVICE_SERIAL.')
        if not self._adb('shell', 'pm', 'path', '--user', '0', PACKAGE).strip():
            raise ConnectionError('Install VIDAA Smart TV on the connected phone first.')
        focus = self._adb('shell', 'dumpsys', 'window')
        if not any(PACKAGE in line and 'U6RemoteActivity' in line for line in focus.splitlines() if 'mCurrentFocus=' in line):
            self._adb('shell', 'am', 'start', '--user', '0', '-f', '0x24000000', '-n', PACKAGE + '/.U6MainActivity')
            time.sleep(1)
        for attempt in range(4):
            self._tree()
            try:
                self._node('linear_menu_ok')
                break
            except ConnectionError:
                try:
                    self._tap(self._node('ll_tab_1'))
                except ConnectionError:
                    if attempt == 3:
                        raise ConnectionError('VIDAA did not open its remote. Check its TV connection on the phone.')
                time.sleep(.5)
        self._tree()
        self._node('linear_menu_ok')
        if self.closed:
            return
        self.ready = True
        self.emit('status', {'state': 'ready', 'text': 'Connected through Android over USB'})
        self.emit('device', dict(self.tv))
        self.emit('sources', [{'sourceid': s, 'displayname': s, 'sourcename': s}
                             for s in ['TV', 'HDMI1', 'HDMI2', 'HDMI3', 'HDMI4', 'AV']])
        self.refresh()

    def _check_focus(self):
        if self.closed or not self.ready:
            raise ConnectionError('Connect your phone first.')
        focus = self._adb('shell', 'dumpsys', 'window')
        lines = [line for line in focus.splitlines() if 'mCurrentFocus=' in line]
        if not any(PACKAGE in line and 'U6RemoteActivity' in line for line in lines):
            self.ready = False
            self.emit('status', {'state': 'offline', 'text': 'VIDAA remote is no longer open on the phone. Reconnect to continue.'})
            raise ConnectionError('Reconnect to reopen the VIDAA remote on the phone.')

    def refresh(self):
        if self.tv.get('description_url'):
            try:
                self.emit('volume', dlna_volume(self.tv))
            except Exception:
                self.emit('volume', None)

    def send_key(self, key):
        self._check_focus()
        if key not in self.supported_keys:
            raise ValueError('This control requires a direct TV pairing.')
        if key in self.KEY_IDS:
            node = self._node(self.KEY_IDS[key])
        else:
            group = self._node('view_volume' if key.startswith('KEY_VOLUME') else 'view_channel')
            node = self._node('image_volume_up' if key.endswith('UP') else 'image_volume_down', group)
        self._tap(node)
        if key in ('KEY_VOLUMEUP', 'KEY_VOLUMEDOWN', 'KEY_MUTE'):
            time.sleep(.25)
            self.refresh()

    def set_source(self, source):
        self._check_focus()
        self._tap(self._node('linear_menu_input'))
        tree = self._tree()
        wanted = source.get('sourcename') or source['sourceid']
        if wanted == 'TV':
            wanted = 'TV'
        for node in tree.iter('node'):
            label = node.get('text', '').strip()
            if node.get('package') == PACKAGE and (label == wanted or (wanted == 'TV' and label in ('TV Channels', 'TV Channels▶'))):
                self._tap(node)
                self._tree()
                return
        self._adb('shell', 'input', 'keyevent', '4')
        self._tree()
        raise ValueError('The phone did not list that input.')

    def close(self):
        self.closed = True
        self.ready = False
        self.actions.shutdown(wait=False, cancel_futures=True)


def wake(tv):
    mac = re.sub('[^0-9a-f]', '', tv.get('mac', '').lower())
    if len(mac) != 12:
        raise ValueError('Discover the TV first so its network address can be saved.')
    packet = b'\xff' * 6 + bytes.fromhex(mac) * 16
    broadcasts = {'255.255.255.255'}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for address in broadcasts:
            for port in (9, 7):
                sock.sendto(packet, (address, port))
