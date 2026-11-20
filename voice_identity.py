"""Identify the Alexa profile on an already verified request."""
import hashlib
import hmac
import json
import os
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import time


def voice_digest(event):
    try:
        s = event['context']['System']
        fields = (s['application']['applicationId'], s['user']['userId'], s['person']['personId'])
        if not all(isinstance(x, str) and 0 < len(x) <= 512 for x in fields):
            return None
        return hashlib.sha256(json.dumps(fields, ensure_ascii=True).encode()).hexdigest()
    except (KeyError, TypeError):
        return None


def matches_voice(event, enrolled):
    actual = voice_digest(event)
    return actual is not None and hmac.compare_digest(actual, enrolled)


def registration_digest(rid):
    return hashlib.sha256(rid.encode('utf-8')).hexdigest()


class VoiceEnrollment:
    def __init__(self, route, path, phrase, expiry, *, aliases=(), clock=time, signed_after=None):
        self.route, self.path, self.expiry, self.clock = route, path, expiry, clock
        self.phrases = frozenset((phrase, *aliases))
        self.signed_after = signed_after

    def __call__(self, event):
        try:
            r = event['request']
            if r['type'] != 'IntentRequest' or r['intent']['name'] != 'AskHermesIntent':
                return None
            value = r['intent']['slots']['question']['value']
            if not isinstance(value, str) or ' '.join(value.lower().split()).rstrip('.!?') not in self.phrases:
                return None
            s = event['context']['System']
            route = (s['application']['applicationId'], s['user']['userId'], s['device']['deviceId'])
            if event['version'] != '1.0' or route != self.route or self.clock() >= self.expiry:
                return 'voice_enrollment_unavailable'
            if self.signed_after is not None:
                signed = datetime.fromisoformat(r['timestamp'].replace('Z', '+00:00'))
                if signed.tzinfo is None or signed.timestamp() < self.signed_after:
                    return 'voice_enrollment_unavailable'
            digest = voice_digest(event)
            if digest is None:
                return 'voice_unrecognized'
            try:
                with NamedTemporaryFile(dir=self.path.parent, delete=False, mode='w', encoding='ascii', newline='\n') as f:
                    temporary = Path(f.name)
                    try:
                        f.write(digest + '\n' + registration_digest(r['requestId']) + '\n')
                        f.flush()
                        os.fsync(f.fileno())
                        f.close()
                        os.link(temporary, self.path)
                    finally:
                        f.close()
                        temporary.unlink(missing_ok=True)
            except FileExistsError:
                return 'voice_enrollment_unavailable'
            return 'voice_enrolled'
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
