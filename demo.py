"""Run a synthetic Alexa → loopback Hermes → loopback HA exercise. No credentials."""
import json
from datetime import datetime, timezone
from time import perf_counter

from bridge import Bridge, Policy
from clients import Hermes, HomeAssistant
from ingress import handle_alexa
from synthetic import json_response, synthetic_server


def run_synthetic():
    memory, runs = {}, {}
    def fake_hermes(path, body, headers):
        if path != '/v1/runs':
            return json_response(runs[path]) if path in runs else (404, {}, b'')
        key = headers['X-Hermes-Session-Key']
        if body['input'] == 'Remember blue lantern':
            memory[key] = 'blue lantern'
            text = 'Stored blue lantern.'
        else:
            text = f'I remember {memory[key]}.' if key in memory else 'I do not remember.'
        run_id = f'run_demo_{len(runs)}'
        runs[f'/v1/runs/{run_id}'] = {'run_id': run_id, 'status': 'completed', 'output': text}
        _, headers, raw = json_response({'run_id': run_id, 'status': 'started'})
        return 202, headers, raw

    def fake_ha(path, *_):
        return json_response([]) if path == '/api/services/notify/send_message' else (404, {}, b'')

    with synthetic_server(fake_hermes) as (hermes_url, hermes_calls), synthetic_server(fake_ha) as (ha_url, ha_calls):
        hermes = Hermes(hermes_url, 'synthetic-hermes', timeout=2)
        ha = HomeAssistant(ha_url, 'synthetic-ha', timeout=2)
        policy = Policy('synthetic-skill', {('synthetic-user', 'synthetic-kitchen'): 'notify.synthetic_kitchen_speak',
                                            ('synthetic-user', 'synthetic-hall'): 'notify.synthetic_hall_speak'})
        bridge = Bridge(policy, hermes, ha.speak)
        ack_ms, sessions = [], []
        try:
            for index, (room, prompt) in enumerate([('kitchen', 'Remember blue lantern'),
                                                     ('kitchen', 'What did I ask you to remember?'),
                                                     ('hall', 'What did I ask you to remember?')], 1):
                sid = f'synthetic-alexa-session-{index}'
                event = {'version': '1.0', 'session': {'sessionId': sid}, 'context': {'System': {
                    'application': {'applicationId': 'synthetic-skill'}, 'user': {'userId': 'synthetic-user'},
                    'device': {'deviceId': f'synthetic-{room}'}},
                }, 'request': {'requestId': f'synthetic-request-{index}', 'type': 'IntentRequest',
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                    'intent': {'name': 'AskHermesIntent', 'slots': {'question': {'value': prompt}}}}}
                raw = json.dumps(event).encode()
                start = perf_counter()
                status, reply = handle_alexa({}, raw, lambda _headers, body: body == raw, bridge.submit_verified)
                ack_ms.append(round((perf_counter() - start) * 1000, 3))
                if status != 200 or 'when it' not in reply['response']['outputSpeech']['text']:
                    raise RuntimeError('synthetic ingress failed to acknowledge')
                sessions.append(sid)
                if not bridge.wait_idle(8):
                    raise RuntimeError('synthetic turn did not finish')
            counts = bridge.counts()
            if counts != {'accepted': 3, 'ha_accepted': 3}:
                raise RuntimeError(f'synthetic pipeline failed: {counts}')
            return {'mode': 'SYNTHETIC ONLY', 'alexa_session_ids': sessions, 'ack_ms': ack_ms,
                    'session_keys': [headers['X-Hermes-Session-Key'] for path, headers, _ in hermes_calls if path == '/v1/runs'],
                    'notifications': [body for _, _, body in ha_calls], 'counts': counts}
        finally:
            bridge.close()


if __name__ == '__main__':
    print(json.dumps(run_synthetic(), indent=2))
