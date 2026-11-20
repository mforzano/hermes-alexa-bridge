"""Loopback-only Alexa skill service. Terminate public TLS at a trusted proxy."""
import argparse
import json
import logging
import os
import re
import signal
import stat
import sys
import warnings
from getpass import GetPassWarning, getpass
from http import HTTPStatus
from pathlib import Path
from threading import Lock
from time import monotonic, time
from urllib.parse import urlsplit

from bridge import Bridge, Policy, identifier
from clients import Hermes, HomeAssistant, OWNER_VOICE_INSTRUCTIONS
from ingress import CERT_URL, MAX_BODY, _not_json, _unique, alexa_sdk_verifier, handle_alexa
from owner_routing import VoiceRouter, read_fingerprint
from voice_identity import VoiceEnrollment

HOST = '127.0.0.1'
PORT = 8765
ROUTE = '/alexa'


def skill_app(verify, submit_verified):
    """WSGI adapter. Only deployment composition supplies the ASK verifier."""
    def app(env, start_response):
        deadline = monotonic() + 5

        def reply(status, payload=None):
            raw = json.dumps(payload if payload is not None else {}).encode('utf-8')
            headers = [('Content-Type', 'application/json'), ('Cache-Control', 'no-store'),
                       ('Content-Length', str(len(raw)))]
            if status == 405:
                headers.append(('Allow', 'POST'))
            start_response(f'{status} {HTTPStatus(status).phrase}', headers)
            return [raw]

        if (env.get('REQUEST_URI') != ROUTE or env.get('PATH_INFO') != ROUTE or env.get('QUERY_STRING')):
            return reply(404)
        if env['REQUEST_METHOD'] != 'POST':
            return reply(405)
        length = env.get('CONTENT_LENGTH', '')
        if not re.fullmatch(r'[0-9]{1,16}', length) or not int(length):
            return reply(400)
        length = int(length)
        if length > MAX_BODY:
            return reply(413)
        if (env.get('CONTENT_TYPE', '').split(';', 1)[0].strip().lower() != 'application/json'
                or env.get('HTTP_CONTENT_ENCODING', 'identity').lower() != 'identity'):
            return reply(415)
        signature = env.get('HTTP_SIGNATURE_256', '')
        cert_url = env.get('HTTP_SIGNATURECERTCHAINURL', '')
        # Waitress combines repeated fields with commas, which neither grammar permits.
        if (not re.fullmatch(r'[A-Za-z0-9+/]{1,2048}={0,2}', signature)
                or len(cert_url) > 512 or not CERT_URL.fullmatch(cert_url)):
            return reply(400)
        try:
            raw = env['wsgi.input'].read(length)
            if len(raw) != length:
                return reply(400)

            def verify_in_time(headers, body):
                return verify(headers, body) is True and monotonic() < deadline

            status, result = handle_alexa({'Signature-256': signature, 'SignatureCertChainUrl': cert_url},
                                          raw, verify_in_time, submit_verified)
        except Exception:
            return reply(500)
        return reply(status, result)
    return app


def listener(app, port):
    from waitress import create_server
    return create_server(app, host=HOST, port=port, threads=2, connection_limit=16, backlog=16,
                         channel_timeout=8, cleanup_interval=1, channel_request_lookahead=0,
                         max_request_header_size=8192, max_request_body_size=MAX_BODY + 1,
                         expose_tracebacks=False, clear_untrusted_proxy_headers=True)


def read_config(path):
    with path.open('rb') as stream:
        mode = os.fstat(stream.fileno()).st_mode
        if not stat.S_ISREG(mode) or (os.name == 'posix' and mode & 0o077):
            raise ValueError('configuration file must be private')
        raw = stream.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024:
        raise ValueError('configuration file too large')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique, parse_constant=_not_json)


def origin_identity(origin):
    url = urlsplit(origin)
    if url.port == 0:
        raise ValueError('invalid upstream port')
    return url.scheme, (url.hostname or '').rstrip('.'), url.port or (443 if url.scheme == 'https' else 80)


def configured_service(path, *, check_only=False, enroll_from=None, enrollment_phrase=None):
    data = read_config(path)
    required = {'routes_file', 'hermes_origin', 'ha_origin'}
    if not isinstance(data, dict) or not required <= data.keys() <= required | {'owner', 'omit_google_maps_urls'}:
        raise ValueError('invalid config fields')
    omit_maps = data.get('omit_google_maps_urls', False)
    if type(omit_maps) is not bool:
        raise ValueError('omit_google_maps_urls must be a JSON boolean')
    routes = Policy.from_config(read_config(path.parent / data['routes_file']))
    for key in ('hermes_origin', 'ha_origin'):
        origin_identity(data[key])
    guest_key = os.environ['HERMES_ALEXA_HERMES_TOKEN']
    h = Hermes(data['hermes_origin'], guest_key)
    ha = HomeAssistant(data['ha_origin'], os.environ['HERMES_ALEXA_HA_TOKEN'])
    if 'owner' not in data:
        if enroll_from is not None:
            raise ValueError('configure the owner API before arming enrollment')
        return None if check_only else Bridge(routes, h, ha.speak, omit_google_maps_urls=omit_maps)

    owner = data['owner']
    if (not isinstance(owner, dict) or owner.keys() != {'hermes_origin', 'fingerprint_file'}
            or not isinstance(owner['fingerprint_file'], str) or not owner['fingerprint_file']):
        raise ValueError('invalid owner configuration')
    owner_key = os.environ['HERMES_ALEXA_OWNER_TOKEN']
    if owner_key == guest_key or origin_identity(owner['hermes_origin']) == origin_identity(data['hermes_origin']):
        raise ValueError('owner and guest require distinct origins and keys')
    owner_client = Hermes(owner['hermes_origin'], owner_key, instructions=OWNER_VOICE_INSTRUCTIONS)
    fingerprint_path = path.parent / owner['fingerprint_file']
    if (not fingerprint_path.parent.is_dir()
            or (os.name == 'posix' and fingerprint_path.parent.stat().st_mode & 0o077)):
        raise ValueError('enrollment needs a private directory')
    try:
        fingerprint, consumed = read_fingerprint(fingerprint_path)
    except FileNotFoundError:
        fingerprint = consumed = None

    enrollment = None
    if enroll_from is not None:
        if fingerprint is not None or check_only:
            raise ValueError('enrollment requires a new fingerprint file and serve mode')
        pair = read_config(enroll_from)
        if not isinstance(pair, dict) or pair.keys() != {'user_id', 'device_id'}:
            raise ValueError('invalid enrollment Echo capture')
        user, device = pair['user_id'], pair['device_id']
        if not all(map(identifier, (user, device))) or (user, device) not in routes.routes:
            raise ValueError('enrollment Echo is not paired')
        if (not isinstance(enrollment_phrase, str)
                or not re.fullmatch(r'register(?: [a-z]+){5,12}', enrollment_phrase)):
            raise ValueError('enrollment phrase must be register followed by 5–12 lowercase words')
        enrollment = VoiceEnrollment((routes.skill_id, user, device), fingerprint_path, enrollment_phrase,
                                     monotonic() + 120, clock=monotonic, signed_after=time())
    return None if check_only else VoiceRouter(routes, h, owner_client, ha.speak, fingerprint, enrollment, consumed,
                                                omit_google_maps_urls=omit_maps)


def capture_one(skill_id, output, *, clock=monotonic):
    """Arm briefly; save only the first fresh signed LaunchRequest for this skill."""
    if (not identifier(skill_id) or not output.parent.is_dir() or os.path.lexists(output)
            or (os.name == 'posix' and output.parent.stat().st_mode & 0o077)):
        raise ValueError('capture needs a new file in a private directory')
    deadline = clock() + 120
    lock = Lock()
    saved = False

    def capture(event):
        nonlocal saved
        try:
            system = event['context']['System']
            request = event['request']
            if (event['version'] != '1.0' or request['type'] != 'LaunchRequest'
                    or system['application']['applicationId'] != skill_id):
                return 'setup'
            user, device = system['user']['userId'], system['device']['deviceId']
            if not all(map(identifier, (user, device, request['requestId']))):
                return 'setup'
        except (KeyError, TypeError):
            return 'setup'
        with lock:
            if not saved and clock() < deadline:
                # Do not overwrite an existing file or follow a symlink at this name.
                fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump({'user_id': user, 'device_id': device}, stream)
                    stream.write('\n')
                saved = True
                return 'paired'
        return 'setup'
    return capture


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('serve', 'check'):
        cmd = sub.add_parser(command)
        cmd.add_argument('--config', type=Path, required=True)
        if command == 'serve':
            cmd.add_argument('--port', type=int, default=PORT, choices=range(1, 65536), metavar='PORT')
            cmd.add_argument('--enroll-from', type=Path, metavar='PAIR_CAPTURE',
                             help='arm one owner enrollment for this paired Echo for 120 seconds')
    cmd = sub.add_parser('capture', help='arm one private identity capture for 120 seconds')
    cmd.add_argument('--skill-id', required=True)
    cmd.add_argument('--output', type=Path, required=True)
    cmd.add_argument('--port', type=int, default=PORT, choices=range(1, 65536), metavar='PORT')
    args = parser.parse_args(argv)

    # Neither HTTP library errors nor SDK exceptions may log raw request data.
    logging.disable(logging.CRITICAL)
    bridge = None
    try:
        from importlib.metadata import version
        import waitress
        if not callable(waitress.create_server):
            raise ImportError('no Waitress listener')
        verify = alexa_sdk_verifier()
        sdk = version('ask-sdk-webservice-support')
        if args.command == 'capture':
            submit = capture_one(args.skill_id, args.output)
        else:
            if args.command == 'check':
                configured_service(args.config, check_only=True)
                print(f'Configuration OK; ASK SDK {sdk}; no network checks.', flush=True)
                return 0
            phrase = None
            if args.enroll_from is not None:
                if not sys.stdin.isatty():
                    raise ValueError('enrollment requires a private interactive terminal')
                with warnings.catch_warnings():
                    warnings.simplefilter('error', GetPassWarning)
                    phrase = getpass('Private enrollment phrase (register followed by 5–12 random lowercase words): ')
            bridge = configured_service(args.config, enroll_from=args.enroll_from, enrollment_phrase=phrase)
            submit = bridge.submit_verified
        http = listener(skill_app(verify, submit), args.port)
        print(f'{args.command}: listening on {HOST}:{args.port}{ROUTE}; ASK SDK {sdk}', flush=True)

        def stop(*_):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stop)
        http.run()
    except KeyboardInterrupt:
        return 0
    except ImportError:
        print('Cannot load ingress dependencies. Install requirements.txt and see the setup guide.', file=sys.stderr)
        return 2
    except Exception:
        print('Bridge could not start or stopped: check private config, credentials, dependencies and port.', file=sys.stderr)
        return 2
    finally:
        if bridge is not None and not bridge.close(timeout=10):
            print('Bridge did not drain; verify unresolved runs privately before restarting.', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
