"""Real Waitress/ASK, synthetic signing trust and private loopback services only."""
import base64
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from unittest.mock import Mock, patch

from ingress import MAX_BODY, alexa_sdk_verifier
from server import capture_one, configured_service, listener, skill_app
from synthetic import json_response, synthetic_server
from test_bridge import envelope
from test_server import CERT, call

HAS_DEPS = all(importlib.util.find_spec(name) for name in ('waitress', 'ask_sdk_webservice_support'))


class CaptureTests(unittest.TestCase):
    def test_only_one_matching_fresh_launch_is_saved_not_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'capture.private.json'
            now = [0]
            capture = capture_one('skill', path, clock=lambda: now[0])
            app = skill_app(lambda *_: True, capture)
            event = envelope()
            call(app, json.dumps(event).encode())  # Intent, not LaunchRequest.
            event['request']['type'] = 'LaunchRequest'
            system = event['context']['System']
            system['application']['applicationId'] = 'wrong-skill'
            call(app, json.dumps(event).encode())
            self.assertFalse(path.exists())
            system['application']['applicationId'] = 'skill'
            saved_time = event['request']['timestamp']
            event['request']['timestamp'] = '2000-01-01T00:00:00Z'
            self.assertEqual(call(app, json.dumps(event).encode())[0], 400)
            self.assertFalse(path.exists())
            event['request']['timestamp'] = saved_time
            now[0] = 120
            call(app, json.dumps(event).encode())
            self.assertFalse(path.exists())
            app = skill_app(lambda *_: True, capture_one('skill', path, clock=lambda: now[0]))
            ids = {'user_id': system['user']['userId'], 'device_id': system['device']['deviceId']}
            status, reply = call(app, json.dumps(event).encode())
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(reply)['response']['outputSpeech']['text'],
                             'Echo pairing recorded.')
            self.assertEqual(json.loads(path.read_bytes()), ids)
            self.assertNotIn(ids['device_id'].encode(), reply)
            self.assertNotIn(ids['user_id'].encode(), reply)
            system['device']['deviceId'] = 'another-device'
            retry_status, retry_reply = call(app, json.dumps(event).encode())
            self.assertEqual(retry_status, 200)
            self.assertEqual(json.loads(retry_reply)['response']['outputSpeech']['text'],
                             'Pairing was not recorded.')
            self.assertEqual(json.loads(path.read_bytes()), ids)
            if os.name == 'posix':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                capture_one('skill', path)


@unittest.skipUnless(HAS_DEPS, 'install requirements.txt for real SDK/Waitress tests')
class InstalledAppTests(unittest.TestCase):
    def test_cli_checks_configuration_and_listens_only_on_loopback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            routes = Path(tmp) / 'routes.private.json'
            routes.write_bytes(Path('examples/routes.example.json').read_bytes())
            path.write_text(json.dumps({'routes_file': routes.name, 'hermes_origin': 'http://127.0.0.1:1',
                                        'ha_origin': 'http://127.0.0.1:2'}))
            for private in (routes, path):
                private.chmod(0o600)
            env = dict(os.environ, HERMES_ALEXA_HERMES_TOKEN='fixture-hermes', HERMES_ALEXA_HA_TOKEN='fixture-ha')
            command = [sys.executable, '-E', '-s', 'server.py']
            check = subprocess.run([*command, 'check', '--config', str(path)], env=env,
                                   capture_output=True, text=True, check=True, timeout=10)
            self.assertIn('ASK SDK 1.3.3', check.stdout)
            del env['HERMES_ALEXA_HA_TOKEN']
            missing = subprocess.run([*command, 'check', '--config', str(path)], env=env,
                                     capture_output=True, text=True, timeout=10)
            self.assertEqual(missing.returncode, 2)
            self.assertNotIn('fixture-hermes', missing.stderr)
            env['HERMES_ALEXA_HA_TOKEN'] = 'fixture-ha'
            with socket.socket() as candidate:
                candidate.bind(('127.0.0.1', 0))
                port = candidate.getsockname()[1]
            with subprocess.Popen([*command, 'serve', '--config', str(path), '--port', str(port)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env) as proc:
                try:
                    line = []
                    reader = Thread(target=lambda: line.append(proc.stdout.readline()), daemon=True)
                    reader.start()
                    reader.join(10)
                    self.assertIn(f'127.0.0.1:{port}/alexa; ASK SDK 1.3.3', ''.join(line))
                    self.assertEqual(request(port, b'{}')[0], 400)
                    self.assertEqual(request(port, b'{}', path='/v1/runs')[0], 404)
                    # The transport must refuse from the declared size alone, before reading a body.
                    self.assertEqual(request(port, b'', content_length=MAX_BODY + 1)[0], 413)
                    self.assertEqual(request(port, b'{}', method='GET')[0], 405)
                finally:
                    proc.terminate()
                    stdout, stderr = proc.communicate(timeout=15)
                self.assertNotIn('fixture-', stdout + stderr)

    def test_real_sdk_enrolls_then_routes_signed_owner_and_guest_over_http(self):
        from ask_sdk_webservice_support import verifier as sdk
        from certvalidator import ValidationContext
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'synthetic ASK')])
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'synthetic CA')])
        now = datetime.now(timezone.utc)

        def certificate(subject, public_key, ca):
            builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(ca_name)
                       .public_key(public_key).serial_number(x509.random_serial_number())
                       .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
                       .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
                       .add_extension(x509.KeyUsage(not ca, False, False, False, False, ca, ca, False, False),
                                      critical=True))
            if not ca:
                builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName('echo-api.amazon.com')]),
                                                critical=False)
            return builder.sign(ca_key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)

        ca = certificate(ca_name, ca_key.public_key(), True)
        cert = certificate(name, key.public_key(), False)
        cert_factory = sdk.CertificateValidator

        def fixture_validator(end, intermediates):
            return cert_factory(end, intermediates, validation_context=ValidationContext(trust_roots=[ca]))

        class FixtureReply(io.BytesIO):
            status, headers = 200, {}

        opener = Mock()
        opener.open.side_effect = lambda *_a, **_kw: FixtureReply(cert)
        fake_runs = {'/v1/runs': {'run_id': 'run_fixture'}, '/v1/runs/run_fixture': {
            'run_id': 'run_fixture', 'status': 'completed', 'output': 'Synthetic answer.'}}
        event = envelope()
        system = event['context']['System']
        system['person'] = {'personId': 'synthetic-owner'}
        phrase = 'register amber violet willow sunflower daffodil'
        event['request']['intent']['slots']['question']['value'] = phrase
        def sign(body):
            return base64.b64encode(key.sign(body, padding.PKCS1v15(), hashes.SHA256())).decode()
        with (synthetic_server(lambda path, *_: json_response(fake_runs[path])) as (guest, guest_calls),
              synthetic_server(lambda path, *_: json_response(fake_runs[path])) as (owner, owner_calls),
              synthetic_server(lambda *_: json_response([])) as (ha, spoken),
              tempfile.TemporaryDirectory() as tmp,
              patch('ingress.build_opener', return_value=opener),
              patch.object(sdk, 'CertificateValidator', side_effect=fixture_validator)):
            root = Path(tmp)
            root.chmod(0o700)
            pair = {'user_id': system['user']['userId'], 'device_id': system['device']['deviceId']}
            for name, data in {
                'bridge.json': {'routes_file': 'routes.json', 'hermes_origin': guest, 'ha_origin': ha,
                                'owner': {'hermes_origin': owner, 'fingerprint_file': 'owner.digest'}},
                'routes.json': {'skill_id': system['application']['applicationId'],
                                'routes': [{**pair, 'entity_id': 'notify.synthetic_speak'}]},
                'pair.json': pair,
            }.items():
                (root / name).write_text(json.dumps(data), encoding='ascii')
                (root / name).chmod(0o600)
            with patch.dict(os.environ, HERMES_ALEXA_HERMES_TOKEN='fixture-guest',
                            HERMES_ALEXA_OWNER_TOKEN='fixture-owner', HERMES_ALEXA_HA_TOKEN='fixture-ha'):
                bridge = configured_service(root / 'bridge.json', enroll_from=root / 'pair.json',
                                            enrollment_phrase=phrase)
            event['request']['timestamp'] = envelope()['request']['timestamp']
            raw = json.dumps(event, indent=2).encode() + b'\n'
            sig = sign(raw)
            http = listener(skill_app(alexa_sdk_verifier(), bridge.submit_verified), 0)
            port = int(http.effective_port)
            thread = Thread(target=http.run, daemon=True)
            thread.start()
            try:
                self.assertEqual(request(port, raw + b' ', signature=sig)[0], 400)
                # The HTTP server must reject case-insensitive duplicate signing headers.
                self.assertEqual(request(port, raw, signature=sig, duplicate=True)[0], 400)
                self.assertFalse((root / 'owner.digest').exists())
                status, reply = request(port, raw, signature=sig)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(reply)['response']['outputSpeech']['text'], 'Voice ID recorded.')
                self.assertEqual(guest_calls + owner_calls, [])
                for person in ('synthetic-owner', 'synthetic-guest'):
                    calls_before_launch = guest_calls + owner_calls
                    spoken_before_launch = len(spoken)
                    session = {'sessionId': 'session-for-' + person, 'new': True}
                    launch = envelope(rid='launch-' + person)
                    launch['session'] = session
                    launch['context']['System']['person'] = {'personId': person}
                    launch['request']['type'] = 'LaunchRequest'
                    del launch['request']['intent']
                    launch_body = json.dumps(launch).encode()
                    self.assertEqual(request(port, launch_body)[0], 400)
                    status, reply = request(port, launch_body, signature=sign(launch_body))
                    self.assertEqual(status, 200)
                    speech = {'type': 'PlainText', 'text': 'What can I help you with?'}
                    self.assertEqual(json.loads(reply)['response'], {
                        'outputSpeech': speech, 'shouldEndSession': False,
                        'reprompt': {'outputSpeech': speech},
                        'directives': [{'type': 'Dialog.ElicitSlot', 'slotToElicit': 'question',
                                        'updatedIntent': {'name': 'AskHermesIntent',
                                                          'confirmationStatus': 'NONE', 'slots': {
                                            'question': {'name': 'question', 'confirmationStatus': 'NONE'}}}}],
                    })
                    self.assertTrue(bridge.wait_idle(4))
                    self.assertEqual(guest_calls + owner_calls, calls_before_launch)
                    self.assertEqual(len(spoken), spoken_before_launch)

                    ordinary = envelope(rid=person, text='question for ' + person)
                    ordinary['session'] = {**session, 'new': False}
                    ordinary['request']['dialogState'] = 'IN_PROGRESS'
                    ordinary['context']['System']['person'] = {'personId': person}
                    body = json.dumps(ordinary).encode()
                    status, reply = request(port, body, signature=sign(body))
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(reply)['response']['outputSpeech']['text'], 'Working on it.')
                    self.assertTrue(bridge.wait_idle(4))
                for lane, calls in (('owner', owner_calls), ('guest', guest_calls)):
                    self.assertEqual([entry[0] for entry in calls], ['/v1/runs', '/v1/runs/run_fixture'])
                    _, headers, payload = calls[0]
                    self.assertEqual(headers['Authorization'], 'Bearer fixture-' + lane)
                    self.assertTrue(headers['X-Hermes-Session-Key'].startswith('alexa:' + lane + ':'))
                    self.assertEqual(payload['input'], 'question for synthetic-' + lane)
                    self.assertEqual('Do not disclose credentials or private information' in payload['instructions'],
                                     lane == 'guest')
                self.assertEqual([entry[2] for entry in spoken], [
                    {'entity_id': 'notify.synthetic_speak', 'message': 'Synthetic answer.'}] * 2)
                opener.open.assert_called_once_with(CERT, timeout=2)
            finally:
                http.task_dispatcher.shutdown()
                http.close()
                thread.join(6)
                bridge.close()


def request(port, raw, *, path='/alexa', method='POST', signature=None, duplicate=False, content_length=None):
    http = HTTPConnection('127.0.0.1', port, timeout=4)
    try:
        http.putrequest(method, path)
        http.putheader('Content-Type', 'application/json')
        http.putheader('Content-Length', str(len(raw) if content_length is None else content_length))
        http.putheader('Connection', 'close')
        if signature:
            http.putheader('Signature-256', signature)
            http.putheader('SignatureCertChainUrl', CERT)
            if duplicate:
                http.putheader('sIgNaTuRe-256', signature)
        http.endheaders(raw)
        response = http.getresponse()
        return response.status, response.read()
    finally:
        http.close()
