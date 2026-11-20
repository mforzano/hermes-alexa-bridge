import unittest
from threading import Event

from bridge import Bridge, Policy, SESSION_KEY
from test_bridge import envelope


def config():
    return {'skill_id': 'skill-a', 'routes': [
        {'user_id': 'user-a', 'device_id': f'device-{dot}', 'entity_id': f'notify.{dot}_speak',
         **({'default_conversation': 'echo'} if dot != 'a' else {})}
        for dot in 'abc']}


class RoutingTests(unittest.TestCase):
    def test_configured_echo_histories_do_not_steer_house_or_each_other(self):
        config_value = config()
        house_running, release = Event(), Event()
        started, steers, spoken = [], [], []
        history = {}

        class SyntheticRuns:
            def start(self, text, _key, conversation):
                previous = history.get(conversation, 'nothing')
                history[conversation] = text
                started.append((text, conversation, previous))
                return text

            def steer(self, run_id, text):
                steers.append((run_id, text))
                return True

            def status(self, run_id):
                if run_id == 'house':
                    house_running.set()
                    if not release.is_set():
                        return {'status': 'running'}
                previous = next(old for text, _, old in started if text == run_id)
                return {'status': 'completed', 'output': f'{run_id} after {previous}'}

        policy = Policy.from_config(config_value)
        bridge = Bridge(policy, SyntheticRuns(), lambda *data: spoken.append(data), poll_interval=0.02)
        try:
            self.assertEqual(bridge.submit_verified(envelope(text='house')), 'accepted')
            self.assertTrue(house_running.wait(2))
            self.assertEqual(bridge.submit_verified(envelope('b1', 'device-b', 'lantern')), 'accepted')
            self.assertEqual(bridge.submit_verified(envelope('c1', 'device-c', 'comet')), 'accepted')
            release.set()
            self.assertTrue(bridge.wait_idle(3))
        finally:
            release.set()
            bridge.close(3)

        # Rebuild from config, change the HA target, and resume the original Echo's history.
        config_value['routes'][1]['entity_id'] = 'notify.renamed_speak'
        bridge = Bridge(Policy.from_config(config_value), SyntheticRuns(), lambda *data: spoken.append(data))
        try:
            self.assertEqual(bridge.submit_verified(envelope('b2', 'device-b', 'recall')), 'accepted')
            self.assertTrue(bridge.wait_idle(3))
        finally:
            bridge.close(3)
        self.assertEqual(steers, [])
        self.assertEqual(len({item[1] for item in started[:3]}), 3)
        self.assertEqual(started[0][1], SESSION_KEY)
        self.assertEqual(started[1][1], started[-1][1])
        self.assertEqual(spoken[-1], ('notify.renamed_speak', 'recall after lantern'))

    def test_refuses_invalid_or_conflicting_device_defaults(self):
        value = config()
        value['routes'].append({**value['routes'][1], 'user_id': 'user-b', 'default_conversation': 'house'})
        with self.assertRaises(ValueError):
            Policy.from_config(value)
        value['routes'].pop()
        value['routes'][1]['default_conversation'] = 'ech0'
        with self.assertRaises(ValueError):
            Policy.from_config(value)
