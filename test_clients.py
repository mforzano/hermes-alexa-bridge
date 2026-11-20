import unittest

from bridge import SESSION_KEY
from clients import Hermes, HomeAssistant, UpstreamError
from synthetic import json_response, synthetic_server


class ClientTests(unittest.TestCase):
    def test_exact_wire_contracts(self):
        def respond(path, body, headers):
            if path == '/v1/runs':
                return 202, {}, b'{"run_id":"run_synthetic","status":"started"}'
            if path == '/v1/runs/run_synthetic':
                return json_response({'run_id': 'run_synthetic', 'status': 'completed', 'output': 'Short answer'})
            return json_response([])
        with synthetic_server(respond) as (base, calls):
            hermes = Hermes(base, 'synthetic-hermes')
            run_id = hermes.start('a question', 'request-digest', SESSION_KEY)
            answer = hermes.status(run_id)
            HomeAssistant(base, 'synthetic-ha').speak('notify.kitchen_speak', answer['output'])
        (hp, hh, hb), (sp, sh, sb), (ap, ah, ab) = calls
        self.assertEqual(hp, '/v1/runs')
        self.assertEqual(sp, '/v1/runs/run_synthetic')
        self.assertIsNone(sb)
        self.assertEqual(sh['Authorization'], 'Bearer synthetic-hermes')
        self.assertEqual(hh['X-Hermes-Session-Key'], SESSION_KEY)
        self.assertEqual(hh['Idempotency-Key'], 'request-digest')
        self.assertEqual(hh['Authorization'], 'Bearer synthetic-hermes')
        self.assertEqual(hb['input'], 'a question')
        self.assertEqual(set(hb), {'input', 'instructions'})
        self.assertFalse({'previous_response_id', 'conversation_history', 'session_id'} & hb.keys())
        self.assertEqual(ap, '/api/services/notify/send_message')
        self.assertEqual(ah['Authorization'], 'Bearer synthetic-ha')
        self.assertEqual(ab, {'entity_id': 'notify.kitchen_speak', 'message': 'Short answer'})

    def test_declines_invalid_run_ids_overlarge_malformed_and_redirected_responses(self):
        answers = [json_response({'run_id': '../etc'}),
                   json_response({'status': 'started'}),
                   (200, {}, b'{' + b' ' * 1_050_000), (200, {}, b'not-json'),
                   (307, {'Location': '/never-forward'}, b'')]
        with synthetic_server(lambda *_: answers.pop(0)) as (base, calls):
            for _ in range(len(answers)):
                with self.assertRaises(UpstreamError):
                    Hermes(base, 'synthetic-hermes').start('test', 'key', SESSION_KEY)
        self.assertEqual([c[0] for c in calls], ['/v1/runs'] * 5)

    def test_refuses_remote_cleartext_and_non_notify_targets(self):
        with self.assertRaises(ValueError):
            Hermes('http://192.0.2.4:8642', 'synthetic-hermes')
        client = HomeAssistant('http://127.0.0.1:1', 'synthetic-ha')
        with self.assertRaises(ValueError):
            client.speak('all', 'no')

    def test_caps_text_and_omits_ssml_tags(self):
        with synthetic_server(lambda *_: json_response([])) as (base, calls):
            HomeAssistant(base, 'synthetic-ha').speak('notify.test_speak', '<audio />' + ' hi' * 1000)
        message = calls[0][2]['message']
        self.assertNotIn('<', message)
        self.assertLessEqual(len(message), 600)
