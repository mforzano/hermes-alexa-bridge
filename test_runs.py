import json
import unittest
from threading import Event, Thread

from bridge import Bridge, Policy, SESSION_KEY
from clients import Hermes, HomeAssistant
from ingress import handle_alexa
from synthetic import json_response, synthetic_server
from collections import deque
from test_bridge import envelope


class RunsTests(unittest.TestCase):
    def test_cross_echo_steer_acks_before_network_and_changes_one_run(self):
        polled, steering, release = Event(), Event(), Event()
        state = {'output': None}

        def fake_hermes(path, body, headers):
            if path == '/v1/runs':
                return 202, {}, b'{"run_id":"run_example","status":"started"}'
            if path == '/v1/runs/run_example/steer':
                steering.set()
                if not release.wait(3):
                    return 500, {}, b''
                state['output'] = 'Corrected: ' + body['input']
                return json_response({'object': 'hermes.run.steer', 'run_id': 'run_example', 'accepted': True})
            if path == '/v1/runs/run_example':
                polled.set()
                return json_response({'run_id': 'run_example', 'status': 'completed' if state['output'] else 'running',
                                      'output': state['output']})
            return 404, {}, b''

        with synthetic_server(fake_hermes) as (url, calls), synthetic_server(lambda *_: json_response([])) as (ha, speech):
            bridge = Bridge(Policy('skill-a', {('user-a', 'device-a'): 'notify.a_speak',
                                               ('user-a', 'device-b'): 'notify.b_speak'}),
                            Hermes(url, 'synthetic-hermes'), HomeAssistant(ha, 'synthetic-ha').speak)
            acked = Event()
            replies = []

            def follow_up():
                raw = json.dumps(envelope('request-2', 'device-b', 'use blue instead')).encode()
                replies.append(handle_alexa({}, raw, lambda *_: True, bridge.submit_verified))
                acked.set()

            try:
                self.assertEqual(bridge.submit_verified(envelope()), 'accepted')
                self.assertTrue(polled.wait(2), 'expected a live Runs API run')
                thread = Thread(target=follow_up, daemon=True)
                thread.start()
                self.assertTrue(steering.wait(2))
                self.assertTrue(acked.wait(1), 'Alexa ack must not wait for steer POST')
                self.assertIn('running conversation', replies[0][1]['response']['outputSpeech']['text'])
                self.assertEqual(speech, [])
                release.set()
                self.assertTrue(bridge.wait_idle(3))
            finally:
                release.set()
                bridge.close(4)
        posts = [(path, headers, body) for path, headers, body in calls if body is not None]
        self.assertEqual([path for path, _, _ in posts], ['/v1/runs', '/v1/runs/run_example/steer'])
        self.assertEqual(posts[0][1]['X-Hermes-Session-Key'], SESSION_KEY)
        self.assertTrue(posts[0][1]['Idempotency-Key'].startswith('alexa-'))
        self.assertEqual(posts[1][2], {'input': 'use blue instead'})
        self.assertEqual([body for _, _, body in speech],
                         [{'entity_id': 'notify.a_speak', 'message': 'Corrected: use blue instead'}])

    def test_ambiguous_create_quarantines_only_its_history(self):
        for reply in (None, (202, {}, b'not json'), json_response({'status': 'started'}), (500, {}, b'')):
            with self.subTest(reply=reply):
                arrived, release = Event(), Event()

                def fake_hermes(path, body, headers):
                    if path != '/v1/runs':
                        return json_response({'run_id': 'run_echo', 'status': 'completed', 'output': 'echo answer'})
                    if body['input'] == 'hello':
                        arrived.set()
                        release.wait(2)
                        return reply
                    return json_response({'run_id': 'run_echo'})

                with synthetic_server(fake_hermes) as (url, calls):
                    spoken = []
                    bridge = Bridge(Policy('skill-a', {('user-a', 'device-a'): 'notify.a_speak',
                                                       ('user-a', 'device-b'): 'notify.b_speak'}, {'device-b': 'echo'}),
                                    Hermes(url, 'synthetic'), lambda *args: spoken.append(args))
                    try:
                        self.assertEqual(bridge.submit_verified(envelope()), 'accepted')
                        self.assertTrue(arrived.wait(2))
                        self.assertEqual(bridge.submit_verified(envelope('queued', text='queued')), 'accepted')
                        self.assertEqual(bridge.submit_verified(envelope('other', 'device-b', 'other')), 'accepted')
                        release.set()
                        self.assertTrue(bridge.wait_idle(3))
                        self.assertEqual([body['input'] for path, _, body in calls if path == '/v1/runs'],
                                         ['hello', 'other'])
                        self.assertEqual(bridge.submit_verified(envelope('later')), 'busy')
                        self.assertEqual(spoken, [('notify.b_speak', 'echo answer')])
                        self.assertEqual(bridge.counts()['run_create_unknown'], 1)
                        self.assertEqual(bridge.counts()['run_unresolved'], 1)
                    finally:
                        release.set()
                        bridge.close(3)

    def scenario(self, dispositions, terminal_fields, requests=('correction',), terminal='completed', event=None,
                 before_terminal=None):
        polled, completed = Event(), Event()
        outcomes = deque(dispositions)
        attempted, starts = [], []

        def fake_hermes(path, body, headers):
            nonlocal before_terminal
            if path == '/v1/runs':
                starts.append((body['input'], headers['X-Hermes-Session-Key'], headers['Idempotency-Key']))
                _, h, raw = json_response({'run_id': f'run_{len(starts)}', 'status': 'started'})
                return 202, h, raw
            if path == '/v1/runs/run_1/steer':
                attempted.append(body['input'])
                code, reply = outcomes.popleft()
                if not outcomes:
                    completed.set()
                _, h, raw = json_response(reply)
                return code, h, raw
            if path == '/v1/runs/run_1/events':
                raw = ': open\n\nid: 2\ndata: ' + json.dumps(event) + '\n\n: stream closed\n\n'
                return 200, {'Content-Type': 'text/event-stream'}, raw.encode()
            run_id = path.split('/')[-1]
            if run_id == 'run_1':
                polled.set()
                if completed.is_set() and before_terminal is not None:
                    response, before_terminal = before_terminal, None
                    return json_response({'run_id': run_id, **response})
                response = ({'status': terminal, 'output': 'original answer', **terminal_fields}
                            if completed.is_set() else {'status': 'running'})
            else:
                response = {'status': 'completed', 'output': 'answer ' + starts[int(run_id[4:]) - 1][0]}
            return json_response({'run_id': run_id, **response})

        with synthetic_server(fake_hermes) as (url, _), synthetic_server(lambda *_: json_response([])) as (ha, speech):
            bridge = Bridge(Policy('skill-a', {('user-a', 'device-a'): 'notify.a_speak',
                                               ('user-a', 'device-b'): 'notify.b_speak'}),
                            Hermes(url, 'synthetic-hermes'), HomeAssistant(ha, 'synthetic-ha').speak)
            try:
                self.assertEqual(bridge.submit_verified(envelope()), 'accepted')
                self.assertTrue(polled.wait(2))
                for index, text in enumerate(requests):
                    update = envelope(f'update-{index}', 'device-b' if index % 2 == 0 else 'device-a', text)
                    self.assertEqual(bridge.submit_verified(update), 'steering')
                    self.assertEqual(bridge.submit_verified(update), 'duplicate')
                self.assertTrue(bridge.wait_idle(4))
            finally:
                completed.set()
                bridge.close(4)
        self.assertEqual(attempted, list(requests))
        self.assertEqual(len({key for _, _, key in starts}), len(starts))
        self.assertEqual({session for _, session, _ in starts}, {SESSION_KEY})
        return [text for text, _, _ in starts], [(b['entity_id'], b['message']) for _, _, b in speech], bridge.counts()

    def test_refusal_followed_immediately_by_terminal_does_not_replay(self):
        for error in ('run_not_accepting_steer', 'steer_not_accepted'):
            for terminal in ('completed', 'interrupted'):
                with self.subTest(error=error, terminal=terminal):
                    starts, speech, counts = self.scenario([(409, {'error': {'code': error}})], {}, terminal=terminal)
                    self.assertEqual(starts, ['hello'])
                    self.assertEqual(speech, [('notify.a_speak', 'original answer')] if terminal == 'completed' else [])
                    self.assertEqual(counts['steer_refused'], 1)

    def test_canonical_pause_prevents_fallback_even_if_controls_disappear_at_terminal(self):
        accepted = (200, {'object': 'hermes.run.steer', 'run_id': 'run_1', 'accepted': True})
        refused = (409, {'error': {'code': 'run_not_accepting_steer'}})
        starts, speech, counts = self.scenario(
            [accepted, refused], {'admission_id': 'adm_synthetic'}, ('pending', 'refused'), terminal='interrupted',
            before_terminal={'status': 'running', 'pending_controls': [{'kind': 'clarification', 'id': 'control_example'}]},
            event={'event': 'run.interrupted', 'run_id': 'run_1', 'pending_steer': 'pending'})
        self.assertEqual(starts, ['hello'])
        self.assertEqual(speech, [])
        self.assertEqual(counts['steer_refused'], 1)
        self.assertEqual(counts.get('steer_fallback', 0), 0)

    def test_only_terminal_pending_suffix_replays_each_original_job(self):
        accepted = (200, {'object': 'hermes.run.steer', 'run_id': 'run_1', 'accepted': True})
        starts, speech, _ = self.scenario([accepted] * 3, {'pending_steer': 'second\nthird'},
                                          ('first', 'second', 'third'), terminal='failed')
        self.assertEqual(starts, ['hello', 'second', 'third'])
        self.assertEqual(speech, [('notify.a_speak', 'answer second'), ('notify.b_speak', 'answer third')])

    def test_ambiguous_post_does_not_retry_or_launch_without_terminal_proof(self):
        for code in (500, 404, 409, 200):
            with self.subTest(code=code):
                starts, speech, counts = self.scenario([(code, {'error': {'code': 'unrecognized'}})], {})
                self.assertEqual(starts, ['hello'])
                self.assertEqual(speech, [('notify.a_speak', 'original answer')])
                self.assertEqual(counts['steer_unknown'], 1)

    def test_terminal_proof_reconciles_ambiguous_post(self):
        starts, speech, _ = self.scenario([(500, {})], {'pending_steer': 'correction'})
        self.assertEqual(starts, ['hello', 'correction'])
        self.assertEqual(speech[-1], ('notify.b_speak', 'answer correction'))

    def test_identical_text_after_ambiguous_post_never_guesses_origin(self):
        accepted = (200, {'object': 'hermes.run.steer', 'run_id': 'run_1', 'accepted': True})
        starts, _, counts = self.scenario([accepted, (500, {})], {'pending_steer': 'same'}, ('same', 'same'))
        self.assertEqual(starts, ['hello'])
        self.assertEqual(counts['steer_unknown'], 2)

    def test_canonical_status_missing_pending_recovers_from_matching_terminal_event(self):
        accepted = (200, {'object': 'hermes.run.steer', 'run_id': 'run_1', 'accepted': True})
        starts, speech, _ = self.scenario([accepted], {'admission_id': 'adm_synthetic'}, event={
            'event': 'run.completed', 'run_id': 'run_1', 'pending_steer': 'correction'})
        self.assertEqual(starts, ['hello', 'correction'])
        self.assertEqual(speech[-1], ('notify.b_speak', 'answer correction'))

    def test_canonical_status_does_not_replay_mismatched_event(self):
        accepted = (200, {'object': 'hermes.run.steer', 'run_id': 'run_1', 'accepted': True})
        starts, _, counts = self.scenario([accepted], {'admission_id': 'adm_synthetic'}, event={
            'event': 'run.completed', 'run_id': 'run_wrong', 'pending_steer': 'correction'})
        self.assertEqual(starts, ['hello'])
        self.assertEqual(counts['steer_unknown'], 1)

    def test_poll_timeout_does_not_turn_definitely_refused_steer_into_parallel_run(self):
        first_poll = Event()
        made = []

        class SyntheticRuns:
            def start(self, text, *_):
                made.append(text)
                return 'run_example'

            def status(self, _):
                first_poll.set()
                return {'status': 'queued'}

            def steer(self, *_):
                return False

        bridge = Bridge(Policy('skill-a', {('user-a', 'device-a'): 'notify.a_speak'}),
                        SyntheticRuns(), lambda *_: None, poll_interval=0.02, run_timeout=0.3)
        try:
            self.assertEqual(bridge.submit_verified(envelope()), 'accepted')
            self.assertTrue(first_poll.wait(1))
            self.assertEqual(bridge.submit_verified(envelope('fix', text='correction')), 'steering')
            self.assertTrue(bridge.wait_idle(2))
            self.assertEqual(made, ['hello'])
            self.assertEqual(bridge.submit_verified(envelope('later')), 'busy')
        finally:
            bridge.close(2)
