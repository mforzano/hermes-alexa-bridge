import json
import os
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from ingress import handle_alexa
import server
from test_bridge import envelope
from voice_identity import voice_digest


def request(person='owner', rid='request', device='device', user='user', skill='skill'):
    event = envelope(device=device, rid=rid)
    event['context']['System']['application']['applicationId'] = skill
    event['context']['System']['user']['userId'] = user
    if person is not None:
        event['context']['System']['person'] = {'personId': person}
    return event


class Runs:
    def __init__(self):
        self.starts, self.steers = [], []
        self.started, self.release = Event(), Event()
        self.steered = Event()
        self.lose_create = False

    def start(self, question, key, conversation):
        self.starts.append((question, key, conversation))
        self.started.set()
        if self.lose_create:
            raise TimeoutError
        return 'run_same_id'

    def status(self, run_id):
        return {'status': 'completed', 'output': 'answer'} if self.release.is_set() else {'status': 'running'}

    def steer(self, run_id, question):
        self.steers.append((run_id, question))
        self.steered.set()
        return True


class OwnerRoutingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.root.chmod(0o700)
        self.path = self.root / 'bridge.json'
        self.data = {'routes_file': 'routes.json', 'ha_origin': 'http://127.0.0.1:1',
                     'hermes_origin': 'http://127.0.0.1:2',
                     'owner': {'hermes_origin': 'http://127.0.0.1:3', 'fingerprint_file': 'owner.digest'}}
        self.write('routes.json', json.dumps({'skill_id': 'skill', 'routes': [
            {'user_id': 'user', 'device_id': 'device', 'entity_id': 'notify.first'},
            {'user_id': 'user', 'device_id': 'other', 'entity_id': 'notify.second'},
            {'user_id': 'another', 'device_id': 'device', 'entity_id': 'notify.first'}]}))
        self.write('owner.digest', voice_digest(request()) + '\n')
        self.env = {'HERMES_ALEXA_HERMES_TOKEN': 'fixture-guest', 'HERMES_ALEXA_OWNER_TOKEN': 'fixture-owner',
                    'HERMES_ALEXA_HA_TOKEN': 'fixture-ha'}

    def write(self, name, text):
        path = self.root / name
        path.write_text(text, encoding='ascii')
        path.chmod(0o600)

    def load(self, **options):
        self.write('bridge.json', json.dumps(self.data))
        with patch.dict(os.environ, self.env):
            return server.configured_service(self.path, **options)

    def bridge(self, **options):
        guest, owner = Runs(), Runs()
        spoken = []
        with patch('server.Hermes', side_effect=[guest, owner]), patch('server.HomeAssistant') as ha:
            ha.return_value.speak.side_effect = lambda entity, text: spoken.append((entity, text))
            service = self.load(**options)
        self.addCleanup(service.close)
        self.addCleanup(owner.release.set)
        self.addCleanup(guest.release.set)
        return service, guest, owner, spoken

    def test_enrollment_immediately_activates_owner_on_next_signed_request(self):
        (self.root / 'owner.digest').unlink()
        self.write('pair.json', json.dumps({'user_id': 'user', 'device_id': 'device'}))
        phrase = 'register fixture amber violet willow sunflower'
        service, guest, owner, _ = self.bridge(enroll_from=self.root / 'pair.json', enrollment_phrase=phrase)
        guest.release.set()
        owner.release.set()

        def send(event, valid=True):
            status, body = handle_alexa({}, json.dumps(event).encode(), lambda *_: valid, service.submit_verified)
            return status, body.get('response', {}).get('outputSpeech', {}).get('text')

        self.assertEqual(send(request(rid='before')), (200, 'Working on it.'))
        self.assertTrue(service.wait_idle(4))
        enroll = request(rid='registration')
        enroll['request']['intent']['slots']['question']['value'] = phrase
        self.assertEqual(send(enroll, False)[0], 400)
        self.assertFalse((self.root / 'owner.digest').exists())
        self.assertEqual(send(enroll), (200, 'Voice ID recorded.'))
        self.assertEqual(send(request(rid='after', device='other')), (200, 'Working on it.'))
        self.assertEqual(send(request('unknown', 'visitor')), (200, 'Working on it.'))
        self.assertTrue(service.wait_idle(4))
        self.assertEqual((len(guest.starts), len(owner.starts)), (2, 1))
        self.assertEqual((self.root / 'owner.digest').read_text().splitlines()[0], voice_digest(enroll))
        self.assertNotIn('Working on it.', send(enroll))

    def test_registration_replay_and_failed_publication(self):
        from datetime import datetime, timedelta, timezone
        path = self.root / 'owner.digest'
        path.unlink()
        self.write('pair.json', json.dumps({'user_id': 'user', 'device_id': 'device'}))
        phrase = 'register fixture amber violet willow sunflower'
        service, guest, owner, _ = self.bridge(enroll_from=self.root / 'pair.json', enrollment_phrase=phrase)
        event = request(rid='registration')
        event['request']['intent']['slots']['question']['value'] = phrase
        before = datetime.now(timezone.utc) - timedelta(seconds=10)
        event['request']['timestamp'] = before.isoformat()
        self.assertEqual(service.submit_verified(event), 'voice_enrollment_unavailable')
        self.assertFalse(path.exists())
        event['request']['timestamp'] = datetime.now(timezone.utc).isoformat()
        with patch('voice_identity.os.link', side_effect=OSError('fixture disk error')):
            self.assertEqual(service.submit_verified(event), 'voice_enrollment_unavailable')
        self.assertFalse(path.exists())
        self.assertEqual(service.submit_verified(event), 'voice_enrolled')
        self.assertEqual(guest.starts + owner.starts, [])
        service.close()
        restarted, guest, owner, _ = self.bridge()
        self.assertEqual(restarted.submit_verified(event), 'duplicate')
        self.assertEqual(guest.starts + owner.starts, [])

    def test_signed_fresh_pair_and_exact_scoped_voice(self):
        service, guest, owner, spoken = self.bridge()
        guest.release.set()
        owner.release.set()

        def send(event, valid=True):
            return handle_alexa({}, json.dumps(event).encode(), lambda *_: valid, service.submit_verified)

        self.assertEqual(send(request(), False)[0], 400)
        stale = request()
        stale['request']['timestamp'] = '2000-01-01T00:00:00Z'
        self.assertEqual(send(stale)[0], 400)
        for event in (request(device='unpaired'), request(skill='other-skill')):
            self.assertIn('couldn', send(event)[1]['response']['outputSpeech']['text'])
        self.assertEqual(owner.starts + guest.starts, [])
        for event in (request(None, 'missing'), request('other', 'unknown'), request(user='another', rid='account'),
                      request(['invalid'], 'type')):
            self.assertEqual(send(event)[1]['response']['outputSpeech']['text'], 'Working on it.')
            self.assertTrue(service.wait_idle(4))
        self.assertEqual(send(request(device='other'))[1]['response']['outputSpeech']['text'], 'Working on it.')
        self.assertTrue(service.wait_idle(4))
        self.assertEqual((len(guest.starts), len(owner.starts)), (4, 1))
        self.assertNotEqual(guest.starts[0][2], owner.starts[0][2])
        self.assertCountEqual(spoken, [('notify.first', 'answer')] * 4 + [('notify.second', 'answer')])

    def test_concurrent_runs_steer_and_play_back_only_in_their_own_lane(self):
        service, guest, owner, spoken = self.bridge()
        self.assertEqual(service.submit_verified(request(rid='same', device='other')), 'accepted')
        self.assertTrue(owner.started.wait(2))
        self.assertEqual(service.submit_verified(request(None, 'same')), 'rejected')
        self.assertEqual(service.submit_verified(request(None, 'guest')), 'accepted')
        self.assertTrue(guest.started.wait(2))
        self.assertEqual(service.submit_verified(request(rid='owner-steer')), 'steering')
        self.assertEqual(service.submit_verified(request(None, 'guest-steer', device='other')), 'steering')
        self.assertTrue(owner.steered.wait(2))
        self.assertTrue(guest.steered.wait(2))
        owner.release.set()
        guest.release.set()
        self.assertTrue(service.wait_idle(4))
        self.assertEqual((len(guest.steers), len(owner.steers)), (1, 1))
        self.assertNotEqual(guest.starts[0][1], owner.starts[0][1])
        self.assertCountEqual(spoken, [('notify.first', 'answer'), ('notify.second', 'answer')])

    def test_uncertain_owner_create_never_falls_back_or_blocks_guest(self):
        service, guest, owner, spoken = self.bridge()
        owner.lose_create = True
        guest.release.set()
        first = request(rid='lost')
        self.assertEqual(service.submit_verified(first), 'accepted')
        self.assertTrue(service.wait_idle(4))
        self.assertEqual(service.submit_verified(first), 'duplicate')
        self.assertEqual(service.submit_verified(request(rid='blocked')), 'busy')
        self.assertEqual(service.submit_verified(request(None, 'guest')), 'accepted')
        self.assertTrue(service.wait_idle(4))
        self.assertEqual((len(guest.starts), len(owner.starts)), (1, 1))
        self.assertEqual(spoken, [('notify.first', 'answer')])

    def test_bad_file_and_shared_upstreams_are_rejected(self):
        for digest in ('x' * 64, '0' * 64 + '\n\n'):
            with self.subTest(digest=digest):
                self.write('owner.digest', digest)
                with self.assertRaises(ValueError):
                    self.load()
        self.write('owner.digest', voice_digest(request()))
        self.data['owner']['hermes_origin'] = self.data['hermes_origin'] + '/'
        with self.assertRaises(ValueError):
            self.load()
        self.data['owner']['hermes_origin'] = 'http://127.0.0.1:3'
        self.env['HERMES_ALEXA_OWNER_TOKEN'] = self.env['HERMES_ALEXA_HERMES_TOKEN']
        with self.assertRaises(ValueError):
            self.load()

    def test_arm_refuses_to_replace_existing_enrollment(self):
        self.write('pair.json', json.dumps({'user_id': 'user', 'device_id': 'device'}))
        before = (self.root / 'owner.digest').read_bytes()
        with self.assertRaises(ValueError):
            self.load(enroll_from=self.root / 'pair.json',
                      enrollment_phrase='register fixture amber violet willow sunflower')
        self.assertEqual((self.root / 'owner.digest').read_bytes(), before)
