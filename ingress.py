"""Transport-independent Alexa ingress. No listener or synthetic bypass here."""
import json

MAX_BODY = 32 * 1024
SPEECH = {
    'accepted': 'I’ll send the reply to this Echo when it’s ready.',
    'steering': 'I received your update for the running conversation. If it needs a separate turn, I’ll reply here.',
    'duplicate': 'I already received that request.',
    'busy': 'Hermes is busy. Please try again shortly.',
    'launch': 'Say, ask a question, followed by your question.',
    'unsupported': 'Try saying, ask a question, followed by your question.',
    'rejected': 'I couldn’t accept that request.',
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
    if not isinstance(event, dict):
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
    """Optional production seam: SDK verifies signature, core checks freshness/IDs.

    Instantiate once in a separately provisioned ingress; SDK is not needed locally.
    The production HTTP adapter is responsible for transport limits and deadlines.
    """
    from ask_sdk_webservice_support.verifier import RequestVerifier
    verifier = RequestVerifier()

    def verify(headers, body):
        # SDK RequestVerifier verifies the UTF-8 re-encoding of this exact text;
        # it does not inspect the deserialized envelope.
        verifier.verify(headers=headers, serialized_request_env=body.decode('utf-8'),
                        deserialized_request_env=None)
        return True
    return verify
