import json
import unittest
from pathlib import Path

from ingress import handle_alexa, MAX_BODY
from test_bridge import envelope


class InteractionModelTests(unittest.TestCase):
    def test_question_samples_and_manual_dialog(self):
        path = Path(__file__).with_name('examples') / 'interaction-model.en-US.json'
        model = json.loads(path.read_text(encoding='utf-8'))['interactionModel']
        language = model['languageModel']
        self.assertEqual(language['invocationName'], 'hermes helper')
        intent = next(i for i in language['intents'] if i['name'] == 'AskHermesIntent')
        self.assertEqual(len(intent['slots']), 1)
        slot = intent['slots'][0]
        self.assertEqual((slot['name'], slot['type']), ('question', 'AMAZON.SearchQuery'))
        self.assertIn('{question}', slot['samples'])
        self.assertTrue({'tell me {question}', 'find out {question}', 'ask a question {question}'}
                        <= set(intent['samples']))
        self.assertNotIn('{question}', [sample.strip() for sample in intent['samples']])
        dialog = model['dialog']
        self.assertEqual(dialog['delegationStrategy'], 'SKILL_RESPONSE')
        dialog_intent = next(i for i in dialog['intents'] if i['name'] == intent['name'])
        dialog_slot = next(s for s in dialog_intent['slots'] if s['name'] == slot['name'])
        self.assertTrue(dialog_slot['elicitationRequired'])
        self.assertFalse(dialog_slot['confirmationRequired'])
        self.assertFalse(dialog_intent['confirmationRequired'])
        prompt = next(p for p in model['prompts'] if p['id'] == dialog_slot['prompts']['elicitation'])
        self.assertIn({'type': 'PlainText', 'value': 'What can I help you with?'}, prompt['variations'])


class IngressTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.body = json.dumps(envelope(), indent=1).encode()

    def submit(self, event):
        self.calls.append(event)
        return 'accepted'

    def test_only_verified_exact_body_is_admitted(self):
        def verifier(headers, body):
            return headers == {'sample': 'synthetic'} and body == self.body
        for body in (self.body + b' ', self.body + b'x' * MAX_BODY):
            status, _ = handle_alexa({'sample': 'synthetic'}, body, verifier, self.submit)
            self.assertIn(status, (400, 413))
        self.assertFalse(self.calls)
        status, response = handle_alexa({'sample': 'synthetic'}, self.body, verifier, self.submit)
        self.assertEqual(status, 200)
        self.assertEqual(response['response']['outputSpeech']['type'], 'PlainText')
        self.assertTrue(response['response']['shouldEndSession'])
        self.assertEqual(self.calls, [json.loads(self.body)])

    def test_malformed_verified_json_never_reaches_bridge(self):
        for body in (b'\xff', b'{"request":1,"request":2}', b'[]', b'{"x": NaN}'):
            self.assertEqual(handle_alexa({}, body, lambda *_: True, self.submit)[0], 400)
        self.assertFalse(self.calls)

    def test_verifier_exception_fails_closed(self):
        def unavailable(*_):
            raise TimeoutError('never returned')
        status, response = handle_alexa({}, self.body, unavailable, self.submit)
        self.assertEqual((status, response), (400, {}))
        self.assertFalse(self.calls)
