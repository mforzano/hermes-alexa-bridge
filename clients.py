"""Private server-to-server clients; neither endpoint is taken from Alexa input."""
import json
import re
from time import monotonic
from unicodedata import category
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from bridge import ENTITY

MAX_RESPONSE = 1024 * 1024
MAX_SPEECH = 600
VOICE_INSTRUCTIONS = ('This is a shared-room voice channel. Answer briefly in plain language suitable for speech. '
                      'Do not disclose credentials or private information or perform consequential actions.')
OWNER_VOICE_INSTRUCTIONS = ('This is an enrolled-owner voice channel, but replies may be overheard. '
                            'Answer briefly in plain language suitable for speech. Use configured tools as appropriate. '
                            'Never speak credentials or bypass any required approval.')


class UpstreamError(Exception):
    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status, self.code = status, code


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class _Endpoint:
    def __init__(self, origin, token, timeout):
        url = urlsplit(origin)
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username is not None
                or url.password is not None or url.path not in ('', '/') or url.query or url.fragment
                or any(not 33 <= ord(c) <= 126 or c == '\\' for c in origin)
                or (url.scheme == 'http' and url.hostname not in ('127.0.0.1', '::1'))):
            raise ValueError('use a bare HTTPS origin or literal loopback HTTP origin')
        if (not isinstance(token, str) or not re.fullmatch(r'[!-~]{1,4096}', token)
                or not 0 < timeout <= 120):
            raise ValueError('invalid upstream credential or timeout')
        self.origin, self._token, self.timeout = origin.rstrip('/'), token, timeout
        self._opener = build_opener(ProxyHandler({}), NoRedirect())

    def post(self, path, data, headers=None, *, response=True):
        return self._request(path, json.dumps(data).encode(), headers, response=response)

    def get(self, path):
        return self._request(path)

    def terminal_event(self, path, run_id, event):
        request = Request(self.origin + path, headers={'Authorization': 'Bearer ' + self._token,
                                                       'Accept': 'text/event-stream'})
        timeout = min(self.timeout, 5)
        deadline, remaining = monotonic() + timeout, MAX_RESPONSE
        try:
            with self._opener.open(request, timeout=timeout) as reply:
                if reply.headers.get_content_type() != 'text/event-stream':
                    raise UpstreamError('expected run event stream')
                data = []
                while remaining and monotonic() < deadline:
                    line = reply.readline(remaining + 1)
                    remaining -= len(line)
                    if not line or remaining < 0:
                        break
                    if line.startswith(b'data:'):
                        data.append(line[5:].strip())
                    elif not line.strip() and data:
                        item = json.loads(b'\n'.join(data))
                        data.clear()
                        if isinstance(item, dict) and item.get('run_id') == run_id and item.get('event') == event:
                            return item
        except HTTPError as error:
            error.close()
        except (OSError, ValueError, RecursionError):
            pass
        raise UpstreamError('no matching terminal run event')

    def _request(self, path, data=None, headers=None, *, response=True):
        request = Request(self.origin + path, data=data, headers={
            'Authorization': 'Bearer ' + self._token, 'Content-Type': 'application/json',
            'Accept': 'application/json', **(headers or {}),
        })
        try:
            with self._opener.open(request, timeout=self.timeout) as reply:
                if not 200 <= reply.status < 300:
                    raise UpstreamError('unsuccessful upstream response')
                if not response:
                    return None
                raw = reply.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise UpstreamError('upstream body too large')
                return json.loads(raw)
        except HTTPError as error:
            code = None
            try:
                raw = error.read(MAX_RESPONSE + 1)
                if len(raw) <= MAX_RESPONSE:
                    code = json.loads(raw)['error']['code']
            except (OSError, ValueError, RecursionError, TypeError, KeyError):
                pass
            finally:
                error.close()
            raise UpstreamError('upstream HTTP failure', error.code, code) from None
        except (OSError, ValueError, RecursionError) as error:
            raise UpstreamError('upstream transport or JSON failure') from None


class Hermes:
    def __init__(self, origin, token, *, timeout=75, instructions=VOICE_INSTRUCTIONS):
        self.endpoint = _Endpoint(origin, token, timeout)
        self.instructions = instructions

    @staticmethod
    def _run_path(run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r'run_[A-Za-z0-9_-]{1,128}', run_id):
            raise UpstreamError('invalid run ID')
        return '/v1/runs/' + run_id

    def start(self, question, request_key, conversation):
        answer = self.endpoint.post('/v1/runs', {'input': question, 'instructions': self.instructions},
                                    {'X-Hermes-Session-Key': conversation, 'Idempotency-Key': request_key})
        run_id = answer.get('run_id') if isinstance(answer, dict) else None
        self._run_path(run_id)
        return run_id

    def status(self, run_id):
        answer = self.endpoint.get(self._run_path(run_id))
        if not isinstance(answer, dict) or answer.get('run_id') != run_id or not isinstance(answer.get('status'), str):
            raise UpstreamError('invalid run status')
        return answer

    def terminal_pending(self, run_id, state):
        event = self.endpoint.terminal_event(self._run_path(run_id) + '/events', run_id, 'run.' + state)
        return event.get('pending_steer')

    def steer(self, run_id, question):
        try:
            answer = self.endpoint.post(self._run_path(run_id) + '/steer', {'input': question})
        except UpstreamError as error:
            if error.status == 409 and error.code in ('run_not_accepting_steer', 'steer_not_accepted'):
                return False
            raise
        if (not isinstance(answer, dict) or answer.get('object') != 'hermes.run.steer'
                or answer.get('run_id') != run_id or answer.get('accepted') is not True):
            raise UpstreamError('invalid steer receipt')
        return True


class HomeAssistant:
    def __init__(self, origin, token, *, timeout=10):
        self.endpoint = _Endpoint(origin, token, timeout)

    def speak(self, entity, text):
        if not ENTITY.fullmatch(entity):
            raise ValueError('expected one notify entity')
        plain = ''.join(' ' if c in '<>&' or category(c).startswith('C') else c for c in text)
        plain = ' '.join(plain.split())
        if not plain:
            raise ValueError('no speakable text')
        if len(plain) > MAX_SPEECH:
            plain = plain[:MAX_SPEECH - 1].rstrip() + '…'
        self.endpoint.post('/api/services/notify/send_message', {'entity_id': entity, 'message': plain},
                           response=False)
