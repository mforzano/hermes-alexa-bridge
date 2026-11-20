import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch

from bridge import Bridge, Policy
from ingress import handle_alexa
from owner_routing import VoiceRouter
from server import configured_service
from synthetic import json_response, synthetic_server
from test_bridge import SyntheticRuns, envelope
from voice_identity import voice_digest


def utterance(rid, text='read more', *, person='speaker-a', device='device-a', dedicated=False):
    event = envelope(rid, device, text)
    if person is not None:
        event['context']['System']['person'] = {'personId': person}
    if dedicated:
        event['request']['intent'] = {'name': 'ReadMoreIntent'}
    return event


def reply(bridge, event, verified=True):
    status, body = handle_alexa({}, json.dumps(event).encode(), lambda *_: verified, bridge.submit_verified)
    return status, body.get('response', {}).get('outputSpeech', {}).get('text')


NO_MORE = 'There’s no more of the last answer to read here.'
LONG_ANSWER = 'An opening sentence. ' + 'A test sentence with several words. ' * 22


class PaginationTests(unittest.TestCase):
    def test_complete_sentence_then_exact_slot_and_dedicated_intent_read_one_page_each(self):
        first = 'The first sentence ends here.'
        url = 'https://example.com/' + 'x' * 495 + '?a=1&b=2'
        middle = 'Visit ' + url + ' for details.'
        last = ('Read the rest of this paragraph. ' * 8).strip()
        output = f'{first} {middle} {last}'
        calls, spoken = [], []

        def run(question, _):
            calls.append(question)
            return output

        bridge = Bridge(Policy('skill-a', {('user-a', 'device-a'): 'notify.a'}),
                        SyntheticRuns(run), lambda *args: spoken.append(args))
        self.addCleanup(bridge.close)
        self.assertEqual(reply(bridge, utterance('q', 'question')), (200, 'Working on it.'))
        self.assertTrue(bridge.wait_idle(2))
        self.assertEqual(len(spoken), 1)
        entity, page = spoken[0]
        self.assertEqual(entity, 'notify.a')
        self.assertTrue(page.startswith(first + ' Say'), page)
        self.assertNotIn('Visit', page)
        self.assertLessEqual(len(page), 600)
        # A bare answer to the slot elicited by LaunchRequest is still a command.
        slot_event = utterance('next', '  Read   MORE  ')
        slot_event['request']['dialogState'] = 'COMPLETED'
        status, page = reply(bridge, slot_event)
        self.assertEqual(status, 200)
        self.assertTrue(page.startswith(middle + ' Say'), page)
        self.assertLessEqual(len(page), 600)
        self.assertEqual(reply(bridge, slot_event), (200, 'I already received that request.'))
        self.assertEqual(reply(bridge, utterance('last', dedicated=True)), (200, last))
        self.assertEqual(reply(bridge, utterance('none'))[1], 'There’s no more of the last answer to read here.')
        self.assertEqual(calls, ['question'])
        self.assertEqual(len(spoken), 1)

    def test_scope_includes_verified_identity_and_device_even_in_shared_guest_history(self):
        policy = Policy('skill-a', {('user-a', 'device-a'): 'notify.a', ('user-a', 'device-b'): 'notify.b'})
        guest, owner = (SyntheticRuns(lambda *_, marker=marker: LONG_ANSWER + marker)
                        for marker in ('guest tail', 'owner tail'))
        origin = utterance('owner-question', 'question')
        router = VoiceRouter(policy, guest, owner, lambda *_: None, voice_digest(origin))
        self.addCleanup(router.close)
        self.assertEqual(reply(router, origin)[1], 'Working on it.')
        self.assertTrue(router.wait_idle(2))
        self.assertEqual(reply(router, utterance('owner-more'), verified=False), (400, None))
        self.assertEqual(reply(router, utterance('other-dot', device='device-b'))[1], NO_MORE)
        self.assertEqual(reply(router, utterance('unpaired', device='unknown'))[1], 'I couldn’t accept that request.')
        self.assertEqual(reply(router, utterance('no-person', person=None))[1], NO_MORE)
        self.assertEqual(reply(router, utterance('visitor', person='visitor'))[1], NO_MORE)
        self.assertEqual(reply(router, utterance('guest-q', 'question', person='visitor'))[1], 'Working on it.')
        self.assertTrue(router.wait_idle(2))
        self.assertEqual(reply(router, utterance('different-guest', person='another'))[1], NO_MORE)
        self.assertTrue(reply(router, utterance('guest-more', person='visitor'))[1].endswith('guest tail'))
        self.assertTrue(reply(router, utterance('owner-more'))[1].endswith('owner tail'))

    def test_concurrent_duplicate_yields_only_one_page_and_expiry_is_absolute(self):
        time = [0]
        bridge = Bridge(Policy('skill-a', {('user-a', 'device-a'): 'notify.a'}),
                        SyntheticRuns(lambda *_: LONG_ANSWER * 4), lambda *_: None, clock=lambda: time[0])
        self.addCleanup(bridge.close)
        self.assertEqual(bridge.submit_verified(utterance('q', 'question')), 'accepted')
        self.assertTrue(bridge.wait_idle(2))
        time[0] = 299
        requested = utterance('same-request')
        with ThreadPoolExecutor(max_workers=2) as threads:
            responses = list(threads.map(lambda _: reply(bridge, requested)[1], range(2)))
        self.assertEqual(responses.count('I already received that request.'), 1)
        self.assertEqual(sum(text.endswith('hear the next part.') for text in responses), 1)
        time[0] = 300
        self.assertEqual(reply(bridge, utterance('expired'))[1], NO_MORE)

    def test_in_flight_ha_success_cannot_restore_a_superseded_answer(self):
        entered, release, started, finish = Event(), Event(), Event(), Event()

        def speak(_, text):
            if text.startswith('An opening'):
                entered.set()
                if not release.wait(3):
                    raise TimeoutError

        def run(text, _):
            if text == 'read more about London':
                started.set()
                finish.wait(3)
                return 'New short answer.'
            return LONG_ANSWER

        bridge = Bridge(Policy('skill-a', {('user-a', 'device-a'): 'notify.a'}), SyntheticRuns(run), speak)
        self.addCleanup(bridge.close)
        self.addCleanup(finish.set)
        self.addCleanup(release.set)
        self.assertEqual(bridge.submit_verified(utterance('old', 'question')), 'accepted')
        self.assertTrue(entered.wait(2))
        self.assertEqual(reply(bridge, utterance('early'))[1], NO_MORE)
        # While the old notification is pending, admit a legitimate non-command question.
        self.assertEqual(bridge.submit_verified(utterance('new', 'read more about London')), 'steering')
        release.set()
        self.assertTrue(started.wait(2))
        self.assertEqual(reply(bridge, utterance('superseded'))[1], NO_MORE)
        finish.set()
        self.assertTrue(bridge.wait_idle(2))
        self.assertEqual(reply(bridge, utterance('new-short'))[1], NO_MORE)

    def test_long_sentence_and_indivisible_url_keep_subsequent_text(self):
        prefix = ('word ' * 121).strip()
        url = 'https://example.com/' + 'd' * 700
        spoken = []
        bridge = Bridge(Policy('skill-a', {('user-a', 'device-a'): 'notify.a'}),
                        SyntheticRuns(lambda *_: f'{prefix} {url} final address.'),
                        lambda _, page: spoken.append(page))
        self.addCleanup(bridge.close)
        self.assertEqual(bridge.submit_verified(utterance('q', 'question')), 'accepted')
        self.assertTrue(bridge.wait_idle(2))
        for index in range(4):
            page = reply(bridge, utterance(f'page-{index}'))[1]
            if page == NO_MORE:
                break
            spoken.append(page)
        self.assertEqual(spoken[-1], 'final address.')
        self.assertEqual(' '.join(spoken).count('word'), 121)
        self.assertTrue(all(len(page) <= 600 and 'example.com' not in page for page in spoken))

    def test_public_maps_config_defaults_off_and_filters_speech_only_in_both_lanes(self):
        output = ('[Cafe North](https://www.google.com/maps/place/Cafe_(North) "Map"), 9 Main St. '
                  'Cafe South <https://maps.app.goo.gl/abc> at 11 Main St. '
                  'Cafe East (https://goo.gl/maps/abc), 12 Main St. '
                  'Cafe West maps.google.co.uk/?q=Cafe+West. '
                  'Keep https://www.google.com/search?q=maps and https://google.com.example/maps.')
        filtered = ('Cafe North, 9 Main St. Cafe South at 11 Main St. Cafe East, 12 Main St. '
                    'Cafe West. Keep https://www.google.com/search?q=maps and https://google.com.example/maps.')

        def runs(path, *_):
            if path == '/v1/runs':
                return 202, {}, b'{"run_id":"run_fixture","status":"started"}'
            return json_response({'run_id': 'run_fixture', 'status': 'completed', 'output': output})

        with (tempfile.TemporaryDirectory() as temp, synthetic_server(runs) as (guest, guest_calls),
              synthetic_server(runs) as (owner, owner_calls),
              synthetic_server(lambda *_: json_response([])) as (ha, speech_calls),
              patch.dict(os.environ, HERMES_ALEXA_HERMES_TOKEN='fixture-guest',
                         HERMES_ALEXA_OWNER_TOKEN='fixture-owner', HERMES_ALEXA_HA_TOKEN='fixture-ha')):
            root = Path(temp)

            def private(name, text):
                path = root / name
                path.write_text(text, encoding='utf-8')
                path.chmod(0o600)
                return path

            private('routes.json', json.dumps({'skill_id': 'skill-a', 'routes': [
                {'user_id': 'user-a', 'device_id': 'device-a', 'entity_id': 'notify.a'}]}))
            private('owner.digest', voice_digest(utterance('enroll')))
            config = {'routes_file': 'routes.json', 'hermes_origin': guest, 'ha_origin': ha}

            def load(**options):
                return configured_service(private('config.json', json.dumps(config)), **options)

            service = load()
            try:
                self.assertEqual(service.submit_verified(utterance('q', 'question')), 'accepted')
                self.assertTrue(service.wait_idle(2))
                self.assertIn('https://www.google.com/maps/place/Cafe_(North)', speech_calls[-1][2]['message'])
            finally:
                service.close()

            for invalid in ('true', 1, None):
                config['omit_google_maps_urls'] = invalid
                with self.assertRaises(ValueError):
                    load(check_only=True)
            config.update(omit_google_maps_urls=True, owner={'hermes_origin': owner, 'fingerprint_file': 'owner.digest'})
            service = load()
            try:
                self.assertEqual(service.submit_verified(utterance('owner-q', 'question')), 'accepted')
                self.assertEqual(service.submit_verified(utterance('guest-q', 'question', person='guest')), 'accepted')
                self.assertTrue(service.wait_idle(2))
                self.assertEqual([call[2]['message'] for call in speech_calls[1:]], [filtered, filtered])
                posts = [call[2]['input'] for call in guest_calls + owner_calls if call[0] == '/v1/runs']
                self.assertEqual(posts, ['question'] * 3)
            finally:
                service.close()

    def test_maps_option_changes_only_speech_with_a_recognized_map_link(self):
        from speech import plain_speech

        for text in ('👍', 'Keep this : and that.'):
            self.assertEqual(plain_speech(text, omit_google_maps_urls=True), text)
        only_link = '<https://maps.app.goo.gl/x>'
        self.assertEqual(plain_speech(only_link, omit_google_maps_urls=True),
                         'There’s no text to read after omitting map links.')

    def test_missing_voice_id_does_not_offer_an_unprotected_continuation(self):
        spoken = []
        bridge = Bridge(Policy('skill-a', {('user-a', 'device-a'): 'notify.a'}),
                        SyntheticRuns(lambda *_: LONG_ANSWER), lambda _, page: spoken.append(page))
        self.addCleanup(bridge.close)
        self.assertEqual(bridge.submit_verified(utterance('q', 'question', person=None)), 'accepted')
        self.assertTrue(bridge.wait_idle(2))
        self.assertEqual(len(spoken), 1)
        self.assertLessEqual(len(spoken[0]), 600)
        self.assertNotIn('Say read more', spoken[0])
        self.assertEqual(reply(bridge, utterance('more', person=None))[1], NO_MORE)
        self.assertEqual(reply(bridge, utterance('identified-later'))[1], NO_MORE)
