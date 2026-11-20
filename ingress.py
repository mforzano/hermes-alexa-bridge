"""Transport-independent Alexa ingress. No listener or synthetic bypass here."""
import json
import re
from threading import Lock
from time import monotonic
from urllib.request import ProxyHandler, build_opener

from bridge import fresh_timestamp
from clients import NoRedirect

# Deliberately support only ordinary Amazon S3 object paths in the ASK namespace.
CERT_URL = re.compile(r'https://s3\.amazonaws\.com(?::443)?/echo\.api/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+', re.ASCII)
CERT_MAX = 64 * 1024

MAX_BODY = 32 * 1024
SPEECH = {
    'accepted': 'Working on it.',
    'steering': 'I received your update for the running conversation. If it needs a separate turn, I’ll reply here.',
    'duplicate': 'I already received that request.',
    'busy': 'Hermes is busy. Please try again shortly.',
    'launch': 'Say, ask a question, followed by your question.',
    'unsupported': 'Try saying, ask a question, followed by your question.',
    'rejected': 'I couldn’t accept that request.',
    'setup': 'Pairing was not recorded.',
    'paired': 'Echo pairing recorded.',
    'voice_enrolled': 'Voice ID recorded.',
    'voice_unrecognized': 'Alexa did not recognize your voice profile.',
    'voice_enrollment_unavailable': 'Voice enrollment is not available on this Echo right now.',
}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _not_json(_):
    raise ValueError('invalid JSON constant')


def handle_alexa(headers, body, verify, submit_verified):
    """Verify original bytes before dispatch. Returns (HTTP status, JSON object)."""
    if not isinstance(body, bytes) or not body:
        return 400, {}
    if len(body) > MAX_BODY:
        return 413, {}
    try:
        if verify(headers, body) is not True:
            return 400, {}
        event = json.loads(body.decode('utf-8'), object_pairs_hook=_unique, parse_constant=_not_json)
    except Exception:
        return 400, {}
    try:
        if not fresh_timestamp(event['request']['timestamp']):
            return 400, {}
    except (KeyError, TypeError):
        return 400, {}
    outcome = submit_verified(event)
    if outcome == 'ended':
        return 200, {'version': '1.0', 'response': {}}
    launch = outcome == 'launch'
    speech = {'type': 'PlainText', 'text': SPEECH[outcome]}
    response = {'outputSpeech': speech, 'shouldEndSession': not launch}
    if launch:
        response['reprompt'] = {'outputSpeech': speech}
    return 200, {'version': '1.0', 'response': response}


def alexa_sdk_verifier():
    """Use the official verifier; replace only its unbounded download/cache."""
    from ask_sdk_webservice_support.verifier import RequestVerifier

    opener = build_opener(ProxyHandler({}), NoRedirect())
    lock = Lock()
    cache = {}

    class BoundedVerifier(RequestVerifier):
        def _load_cert_chain(self, cert_url):
            if (len(cert_url) > 512 or not CERT_URL.fullmatch(cert_url)
                    or cert_url.rsplit('/', 1)[-1] in {'.', '..'}):
                raise ValueError('unsupported certificate URL')
            with lock:
                now = monotonic()
                for url, (expiry, _) in list(cache.items()):
                    if now >= expiry:
                        del cache[url]
                if cert_url in cache:
                    return cache[cert_url][1]
            deadline = monotonic() + 3
            with opener.open(cert_url, timeout=2) as reply:
                if reply.status != 200 or reply.headers.get('Content-Encoding', 'identity') != 'identity':
                    raise ValueError('unexpected certificate response')
                data = bytearray()
                while len(data) <= CERT_MAX and monotonic() < deadline:
                    block = reply.read1(min(8192, CERT_MAX + 1 - len(data)))
                    if not block:
                        break
                    data.extend(block)
                if not data or len(data) > CERT_MAX or monotonic() >= deadline:
                    raise ValueError('certificate download over limit')
            result = bytes(data)
            with lock:
                if len(cache) >= 4:
                    del cache[next(iter(cache))]
                cache[cert_url] = (monotonic() + 300, result)
            return result

    verifier = BoundedVerifier()

    def verify(headers, body):
        # SDK RequestVerifier verifies the UTF-8 re-encoding of this exact text;
        # it does not inspect the deserialized envelope.
        verifier.verify(headers=headers, serialized_request_env=body.decode('utf-8'),
                        deserialized_request_env=None)
        return True
    return verify
