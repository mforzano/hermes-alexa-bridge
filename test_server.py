"""Loopback adapter tests; verifier fixtures are synthetic, never CLI options."""
import io
import json
import unittest
from test_bridge import envelope
from unittest.mock import Mock


class IngressTests(unittest.TestCase):
    def test_raw_bytes_reach_verifier_before_dispatch(self):
        from server import skill_app
        raw = json.dumps(envelope(), indent=2, ensure_ascii=False).encode() + b'\n'
        verify = Mock(return_value=True)
        submit = Mock(return_value='launch')
        status, _ = call(skill_app(verify, submit), raw)
        self.assertEqual(status, 200)
        verify.assert_called_once_with({'Signature-256': 'YQ==',
                                        'SignatureCertChainUrl': CERT}, raw)
        submit.assert_called_once_with(json.loads(raw))

    def test_invalid_transport_never_verifies(self):
        from ingress import MAX_BODY
        from server import skill_app
        verify, submit = Mock(), Mock()
        app = skill_app(verify, submit)
        for overrides, status in [
            ({'REQUEST_URI': '/%61lexa'}, 404), ({'QUERY_STRING': 'debug=1'}, 404),
            ({'REQUEST_METHOD': 'GET'}, 405), ({'HTTP_SIGNATURE_256': ''}, 400),
            ({'HTTP_SIGNATURE_256': 'YQ==, YQ=='}, 400),
            ({'HTTP_SIGNATURECERTCHAINURL': CERT + ', ' + CERT}, 400),
            ({'CONTENT_LENGTH': str(MAX_BODY + 1)}, 413), ({'CONTENT_LENGTH': '3'}, 400),
            ({'HTTP_CONTENT_ENCODING': 'gzip'}, 415),
        ]:
            with self.subTest(overrides=overrides):
                self.assertEqual(call(app, **overrides)[0], status)
        verify.assert_not_called()
        submit.assert_not_called()

    def test_stale_signed_envelope_never_dispatches(self):
        from server import skill_app
        event = envelope()
        event['request']['timestamp'] = '2000-01-01T00:00:00Z'
        submit = Mock()
        self.assertEqual(call(skill_app(lambda *_: True, submit), json.dumps(event).encode())[0], 400)
        submit.assert_not_called()


def call(app, raw=b'{}', **overrides):
    env = {'REQUEST_METHOD': 'POST', 'PATH_INFO': '/alexa', 'REQUEST_URI': '/alexa',
           'QUERY_STRING': '', 'CONTENT_TYPE': 'application/json', 'CONTENT_LENGTH': str(len(raw)),
           'HTTP_SIGNATURE_256': 'YQ==', 'HTTP_SIGNATURECERTCHAINURL': CERT,
           'wsgi.input': io.BytesIO(raw), **overrides}
    result = []
    out = b''.join(app(env, lambda status, headers: result.append(int(status[:3]))))
    return result[0], out


CERT = 'https://s3.amazonaws.com/echo.api/echo-api-cert.pem'
