import tempfile
import unittest
from pathlib import Path
from voice_identity import VoiceEnrollment, matches_voice


def event(device='living', person='mike', phrase='register blue tulip'):
    return {'version': '1.0', 'context': {'System': {'application': {'applicationId': 'skill'},
        'user': {'userId': 'user'}, 'device': {'deviceId': device}, 'person': {'personId': person}}},
        'request': {'type': 'IntentRequest', 'requestId': 'fixture', 'intent': {'name': 'AskHermesIntent',
            'slots': {'question': {'value': phrase}}}}}


class VoiceIdentityTests(unittest.TestCase):
    def test_capture_is_scoped_once_and_compares_speaker_not_device(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'voice'
            enroll = VoiceEnrollment(('skill', 'user', 'living'), p, 'register blue tulip', 20, clock=lambda: 10)
            self.assertIsNone(enroll(event(phrase='weather')))
            self.assertEqual(enroll(event(device='bath')), 'voice_enrollment_unavailable')
            self.assertEqual(enroll(event(person='')), 'voice_unrecognized')
            self.assertFalse(p.exists())
            self.assertEqual(enroll(event()), 'voice_enrolled')
            saved = p.read_text().splitlines()[0]
            self.assertNotIn('mike', saved)
            self.assertTrue(matches_voice(event(device='bath'), saved))
            self.assertFalse(matches_voice(event(person='other'), saved))
            self.assertEqual(enroll(event(person='other')), 'voice_enrollment_unavailable')
            self.assertEqual(p.read_text().splitlines()[0], saved)

    def test_explicit_transcription_variant_matches_without_fuzzy_matching(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'voice'
            enroll = VoiceEnrollment(('skill', 'user', 'living'), p, 'register orange amber violet', 20,
                                     aliases=('register orange amber violets',), clock=lambda: 10)
            self.assertIsNone(enroll(event(phrase='register orange amber blue')))
            self.assertEqual(enroll(event(phrase='Register orange amber violets.')), 'voice_enrolled')

    def test_expiry_does_not_enroll(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'voice'
            enroll = VoiceEnrollment(('skill', 'user', 'living'), p, 'register blue tulip', 10, clock=lambda: 10)
            self.assertEqual(enroll(event()), 'voice_enrollment_unavailable')
            self.assertFalse(p.exists())
