import unittest

from demo import run_synthetic
from bridge import SESSION_KEY


class DemoTests(unittest.TestCase):
    def test_independent_alexa_sessions_continue_same_conversation_and_route_to_origin(self):
        result = run_synthetic()
        self.assertEqual(result['session_keys'], [SESSION_KEY] * 3)
        self.assertEqual(len(set(result['alexa_session_ids'])), 3)
        self.assertEqual(result['notifications'], [
            {'entity_id': 'notify.synthetic_kitchen_speak', 'message': 'Stored blue lantern.'},
            {'entity_id': 'notify.synthetic_kitchen_speak', 'message': 'I remember blue lantern.'},
            {'entity_id': 'notify.synthetic_hall_speak', 'message': 'I remember blue lantern.'},
        ])
        self.assertTrue(all(0 <= ms < 8000 for ms in result['ack_ms']))
        self.assertEqual(result['counts'], {'accepted': 3, 'ha_accepted': 3})
