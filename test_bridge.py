import json
import unittest
from datetime import datetime, timezone
from threading import Event
from time import monotonic

from bridge import Bridge, Policy
from ingress import handle_alexa


class SyntheticRuns:
    def __init__(self, ask):
        self.ask = ask
        self.answers = {}

    def start(self, question, key, _conversation):
        self.answers[key] = self.ask(question, key)
        return key

    def status(self, run_id):
        output = self.answers.pop(run_id)
        return {'status': 'failed' if output is None else 'completed', 'output': output}


def envelope(rid='request-1', device='device-a', text='hello'):
    return {
        'version': '1.0',
        'context': {'System': {'application': {'applicationId': 'skill-a'},
                   'user': {'userId': 'user-a'}, 'device': {'deviceId': device}}},
        'request': {'type': 'IntentRequest', 'requestId': rid,
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                    'intent': {'name': 'AskHermesIntent', 'slots': {'question': {'value': text}}}},
    }


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.called = []
        self.entered = Event()
        self.release = Event()

    def run_hermes(self, question, idem):
        self.called.append(('Hermes', question, idem))
        self.entered.set()
        if not self.release.wait(5):
            raise TimeoutError
        return 'answer ' + question

    def create(self, **options):
        bridge = Bridge(Policy('skill-a', {('user-a', 'device-a'): 'notify.a_speak',
                                          ('user-a', 'device-b'): 'notify.b_speak'}),
                        SyntheticRuns(self.run_hermes), lambda entity, text: self.called.append((entity, text)), **options)
        self.addCleanup(bridge.close)
        self.addCleanup(self.release.set)
        return bridge

    def test_ack_while_hermes_blocked_and_fifo_routes_to_origin(self):
        b = self.create()
        start = monotonic()
        self.assertEqual(b.submit_verified(envelope()), 'accepted')
        self.assertLess(monotonic() - start, 1)
        self.assertTrue(self.entered.wait(1))
        second = envelope('request-2', 'device-b', 'follow up')
        second['entity_id'] = 'notify.attacker'
        self.assertEqual(b.submit_verified(second), 'accepted')
        self.assertEqual(len(self.called), 1)
        self.release.set()
        self.assertTrue(b.wait_idle(3))
        self.assertEqual([x[:2] for x in self.called], [('Hermes', 'hello'), ('notify.a_speak', 'answer hello'),
                                                        ('Hermes', 'follow up'), ('notify.b_speak', 'answer follow up')])

    def test_exact_duplicate_conflict_and_busy(self):
        b = self.create(max_pending=1, max_seen=1)
        event = envelope()
        self.assertEqual(b.submit_verified(event), 'accepted')
        self.assertEqual(b.submit_verified(event), 'duplicate')
        self.assertEqual(b.submit_verified(envelope(text='different')), 'rejected')
        self.assertEqual(b.submit_verified(envelope('request-2')), 'busy')
        self.release.set()
        self.assertTrue(b.wait_idle(3))
        self.assertEqual(b.submit_verified(event), 'duplicate')
        self.assertEqual(b.submit_verified(envelope('request-2')), 'busy')
        self.assertEqual(len(self.called), 2)

    def test_reject_wrong_skill_user_device_and_stale_input(self):
        b = self.create()
        wrong_skill, wrong_user, stale = envelope(), envelope(), envelope()
        wrong_skill['context']['System']['application']['applicationId'] = 'other'
        wrong_user['context']['System']['user']['userId'] = 'other'
        stale['request']['timestamp'] = '2000-01-01T00:00:00Z'
        bad = [wrong_skill, wrong_user, envelope(device='other'), stale, None, envelope(text='x' * 1001), envelope(text='\ud800')]
        for item in bad:
            with self.subTest(item=repr(item)[:100]):
                self.assertEqual(b.submit_verified(item), 'rejected')
        self.assertFalse(self.called)

    def test_missing_question_reelicits_without_admitting_work(self):
        b = self.create()
        for fields in ({}, {'slots': {}}, {'slots': {'question': {'name': 'question'}}}):
            with self.subTest(fields=fields):
                event = envelope()
                event['request']['intent'] = {'name': 'AskHermesIntent', **fields}
                event['request']['dialogState'] = 'STARTED'
                status, reply = handle_alexa({}, json.dumps(event).encode(), lambda *_: True, b.submit_verified)
                self.assertEqual(status, 200)
                self.assertFalse(reply['response']['shouldEndSession'])
                self.assertEqual(reply['response']['directives'][0]['slotToElicit'], 'question')
        self.assertEqual(b.counts().get('accepted', 0), 0)
        self.assertFalse(self.called)

    def test_missing_question_outside_active_dialog_is_rejected(self):
        b = self.create()
        event = envelope()
        del event['request']['intent']['slots']
        self.assertEqual(b.submit_verified(event), 'rejected')
        event['request']['dialogState'] = 'COMPLETED'
        self.assertEqual(b.submit_verified(event), 'rejected')
        self.assertEqual(b.counts().get('accepted', 0), 0)

    def test_cancel_and_stop_end_only_the_alexa_session(self):
        b = self.create()
        self.assertEqual(b.submit_verified(envelope()), 'accepted')
        self.assertTrue(self.entered.wait(1))
        for name in ('AMAZON.CancelIntent', 'AMAZON.StopIntent'):
            event = envelope(rid=name)
            event['request']['intent'] = {'name': name}
            status, reply = handle_alexa({}, json.dumps(event).encode(), lambda *_: True, b.submit_verified)
            self.assertEqual(status, 200)
            self.assertEqual(reply['response'], {
                'outputSpeech': {'type': 'PlainText', 'text': 'Goodbye.'}, 'shouldEndSession': True,
            })
        self.release.set()
        self.assertTrue(b.wait_idle(3))
        self.assertEqual([call[:2] for call in self.called], [('Hermes', 'hello'), ('notify.a_speak', 'answer hello')])

    def test_malformed_question_is_rejected_without_admitting_work(self):
        b = self.create()
        for slots in (None, [], {'question': None}, {'question': []},
                      *({'question': {'value': v}} for v in (None, 5, '', ' \t ', 'a\x00b'))):
            with self.subTest(slots=slots):
                event = envelope()
                event['request']['intent']['slots'] = slots
                self.assertEqual(b.submit_verified(event), 'rejected')
        self.assertEqual(b.counts().get('accepted', 0), 0)
        self.assertFalse(self.called)

    def test_failed_hermes_has_no_notification_and_worker_continues(self):
        original = self.run_hermes
        def fail_first(text, key):
            if text == 'fail':
                return None  # Known terminal failure, not an ambiguous create.
            return original(text, key)
        self.run_hermes = fail_first
        b = self.create()
        self.release.set()
        self.assertEqual(b.submit_verified(envelope(text='fail')), 'accepted')
        self.assertEqual(b.submit_verified(envelope('request-2')), 'accepted')
        self.assertTrue(b.wait_idle(3))
        self.assertEqual(b.counts()['hermes_failed'], 1)
        self.assertEqual(self.called[-1], ('notify.a_speak', 'answer hello'))

    def test_queued_job_expiry_drops_work_without_speaking(self):
        now = [100.0]
        b = self.create(clock=lambda: now[0], queue_max_age=10)
        self.assertEqual(b.submit_verified(envelope()), 'accepted')
        self.assertTrue(self.entered.wait(1))
        self.assertEqual(b.submit_verified(envelope('request-2')), 'accepted')
        now[0] += 11
        self.release.set()
        self.assertTrue(b.wait_idle(3))
        self.assertEqual(b.counts()['expired'], 1)
        self.assertEqual(len(self.called), 2)
