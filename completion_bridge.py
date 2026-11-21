"""Opt-in relay from Hermes profile-local detached receipts to a pinned Photon DM."""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from contextlib import closing, contextmanager
from pathlib import Path

# Hermes ae2a81ccc0: session_api.py, hermes_state_common.py,
# hermes_state_compression.py, hermes_state_detached_results.py, gateway/wake.py.
DECLARED = 'gateway.api.conversation.v1.'
BINDING = 'gateway.api.binding.v1.'
OWNER_KEY = re.compile(r'alexa:owner:(?:household:voice:v1|echo:voice:v1:[0-9a-f]{64})')
KIND = 'async_delegation_complete'
OUTCOMES = {'accepted', 'rejected', 'ambiguous'}
BATCH = 16


def _mapping(raw):
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def _meta(db, key):
    row = db.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
    return row[0] if row else None


def _session_ids(db, keys):
    found = set()
    for key in keys:
        root = _meta(db, DECLARED + key)
        if root is None:  # A configured room need not have had its first turn yet.
            continue
        receipt = _mapping(_meta(db, BINDING + root)) or {}
        entry = receipt.get('entry')
        origin = entry.get('origin') if isinstance(entry, dict) else None
        route = receipt.get('route')
        row = db.execute('SELECT * FROM sessions WHERE id=?', (root,)).fetchone()
        if (not row or not route or not receipt.get('profile_id')
                or receipt.get('storage_source', 'api_server') != 'api_server'
                or (receipt.get('declared_key'), receipt.get('session_id')) != (key, root)
                or not isinstance(origin, dict)
                or (entry.get('session_id'), entry.get('session_key'), entry.get('platform')) != (root, route, 'api_server')
                or tuple(origin.get(k) for k in ('platform', 'chat_id', 'user_id', 'chat_type'))
                != ('api_server', root, 'api', 'dm')):
            raise ValueError('owner API binding cannot be verified')

        def same_origin(candidate):
            return ((candidate['source'], candidate['session_key'], candidate['chat_id'],
                     candidate['user_id'], candidate['chat_type']) == ('api_server', route, root, 'api', 'dm')
                    and _mapping(candidate['origin_json']) == origin)

        if not same_origin(row):
            raise ValueError('owner API session disagrees with its binding')
        # Follow only an unambiguous compression edge retaining the exact API route and origin.
        # Never follow forks, resets, independently bound children, or generic parent links.
        for _ in range(1001):
            sid = row['id']
            if sid in found:
                raise ValueError('overlapping or cyclic owner session lineage')
            found.add(sid)
            if row['end_reason'] != 'compression':
                break
            children = []
            for child in db.execute('SELECT * FROM sessions WHERE parent_session_id=?', (sid,)):
                cfg = {} if child['model_config'] is None else _mapping(child['model_config'])
                if (same_origin(child) and cfg is not None
                        and not any(k in cfg for k in ('_branched_from', '_delegate_from', '_reset_from'))
                        and _meta(db, BINDING + child['id']) is None):
                    children.append(child)
            if len(children) > 1:
                raise ValueError('ambiguous owner compression lineage')
            if not children:
                break
            row = children[0]
        else:
            raise ValueError('owner compression lineage exceeds limit')
    return sorted(found)


def _completion(raw):
    data = _mapping(raw)
    if not data or data.get('delivery_notice') or data.get('presentation_suppressed'):
        return None
    unit = data.get('delegation_id')
    counts = [data.get(k) for k in ('task_count', 'completed_count', 'failed_count')]
    if (not isinstance(unit, str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,160}', unit)
            or any(type(n) is not int or not 0 <= n <= 9999 for n in counts)
            or not counts[0] or counts[0] != sum(counts[1:])):
        return None
    return hashlib.sha256(unit.encode('ascii')).hexdigest(), (
        f'Alexa background work finished: {counts[1]} completed, {counts[2]} failed.')


@contextmanager
def _writer(db):
    db.execute('BEGIN IMMEDIATE')
    with db:
        yield


class Relay:
    def __init__(self, source, ledger, owner_keys, target):
        if (not isinstance(owner_keys, (list, tuple)) or not 1 <= len(owner_keys) <= 65
                or not all(isinstance(k, str) and OWNER_KEY.fullmatch(k) for k in owner_keys)
                or len(set(owner_keys)) != len(owner_keys)):
            raise ValueError('explicit owner Alexa conversation keys are required')
        self.source, self.ledger = Path(source).resolve(), Path(ledger).resolve()
        if self.source == self.ledger or (self.ledger.exists() and self.ledger.samefile(self.source)):
            raise ValueError('source and relay ledger must be separate files')
        self.keys = sorted(owner_keys)
        self.target = target

    @contextmanager
    def _snapshot(self):
        before = self.source.stat()
        with closing(sqlite3.connect(self.source.as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            schema = db.execute('SELECT version FROM schema_version').fetchall()
            ddl = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'").fetchone()
            if [r[0] for r in schema] != [33] or not ddl or 'AUTOINCREMENT' not in ddl[0].upper():
                raise ValueError('unsupported Hermes state database contract')
            seq = db.execute("SELECT seq FROM sqlite_sequence WHERE name='messages'").fetchone()
            high = seq[0] if seq else 0
            if not before.st_ino or (before.st_dev, before.st_ino) != (self.source.stat().st_dev, self.source.stat().st_ino):
                raise ValueError('source database identity is unavailable or changed')
            identity = hashlib.sha256(json.dumps(
                [1, str(self.source), before.st_dev, before.st_ino, self.keys, self.target]
            ).encode()).hexdigest()
            yield db, identity, high, _session_ids(db, self.keys)

    def _open_ledger(self, *, create=False):
        mode = 'rwc' if create else 'rw'
        if not create and not self.ledger.is_file():
            raise ValueError('relay has not been activated')
        db = sqlite3.connect(self.ledger.as_uri() + '?mode=' + mode, uri=True, timeout=10)
        db.execute('PRAGMA synchronous=FULL')
        return db

    @staticmethod
    def _rows(db, ids, after, high, limit):
        if not ids:
            return iter(())
        marks = ','.join('?' for _ in ids)
        return db.execute(f'''SELECT id, display_metadata FROM messages
            WHERE id>? AND id<=? AND session_id IN ({marks}) AND role='user'
            AND display_kind=? ORDER BY id LIMIT ?''', (after, high, *ids, KIND, limit))

    def activate(self):
        """Explicit no-send snapshot; seed unit dedup for history cloned after activation."""
        with closing(self._open_ledger(create=True)) as out, _writer(out):
            out.execute('CREATE TABLE IF NOT EXISTS cursor_state(slot INTEGER PRIMARY KEY CHECK(slot=1), identity TEXT, high INTEGER)')
            out.execute('CREATE TABLE IF NOT EXISTS deliveries(unit TEXT PRIMARY KEY, row_id INTEGER, outcome TEXT)')
            if out.execute('SELECT 1 FROM cursor_state').fetchone():
                raise ValueError('relay ledger is already activated')
            with self._snapshot() as (db, identity, high, ids):
                for row in self._rows(db, ids, 0, high, -1):
                    item = _completion(row['display_metadata'])
                    if item:
                        out.execute('INSERT OR IGNORE INTO deliveries VALUES(?,?,?)', (item[0], row['id'], 'baseline'))
                out.execute('INSERT INTO cursor_state VALUES(1,?,?)', (identity, high))

    @staticmethod
    def _cursor(out, identity, high):
        row = out.execute('SELECT identity,high FROM cursor_state WHERE slot=1').fetchone()
        if row is None or row[0] != identity or row[1] > high:
            raise ValueError('relay source, cursor, or destination changed; stop and review')
        return row[1]

    def poll(self, send):
        outcomes = Counter()
        with closing(self._open_ledger()) as out:
            with self._snapshot() as (db, identity, high, ids):
                after = self._cursor(out, identity, high)
                rows = [(r['id'], _completion(r['display_metadata']))
                        for r in self._rows(db, ids, after, high, BATCH)]
            for row_id, item in rows:
                with _writer(out):
                    # Commit ambiguity and cursor together before any external write. A crash on
                    # either side of transmission is never automatically retried.
                    if row_id <= self._cursor(out, identity, high):
                        continue
                    claimed = item and out.execute('INSERT OR IGNORE INTO deliveries VALUES(?,?,?)',
                                                   (item[0], row_id, 'ambiguous')).rowcount == 1
                    out.execute('UPDATE cursor_state SET high=? WHERE slot=1', (row_id,))
                if claimed:
                    result = send(item[1])
                    if result not in OUTCOMES:
                        raise ValueError('unrecognized send outcome; acceptance unknown')
                    with out:
                        out.execute('UPDATE deliveries SET outcome=? WHERE unit=?', (result, item[0]))
                    outcomes[result] += 1
            if len(rows) < BATCH:
                with _writer(out):
                    out.execute('UPDATE cursor_state SET high=max(high,?) WHERE slot=1', (high,))
        return dict(outcomes)

    def status(self):
        with closing(self._open_ledger()) as out:
            return dict(out.execute("SELECT outcome,count(*) FROM deliveries WHERE outcome!='baseline' GROUP BY outcome"))


class Photon:
    def __init__(self, executable, home, target, *, run=subprocess.run):
        if not isinstance(target, str) or not re.fullmatch(r'any;-;\+[0-9]{6,15}', target):
            raise ValueError('an exact native Photon DM ID is required')
        self.executable, self.home, self.target, self.run = executable, home, target, run

    def _call(self, *args, payload=None):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith('PHOTON_')}
        env.update(HERMES_HOME=str(self.home), PYTHONIOENCODING='utf-8')
        return self.run([str(self.executable), 'send', *args], input=payload,
                        capture_output=True, timeout=45, env=env)

    def check_route(self):
        result = self._call('--list', 'photon', '--json')
        body = _mapping(result.stdout) or {}
        platforms = body.get('platforms')
        rows = platforms.get('photon') if isinstance(platforms, dict) else None
        matches = [r for r in rows if isinstance(r, dict) and r.get('id') == self.target] if isinstance(rows, list) else []
        if (result.returncode or len(matches) != 1 or matches[0].get('type') != 'dm'
                or 'thread_id' not in matches[0] or matches[0]['thread_id'] is not None):
            raise ValueError('configured Photon route was not verified as one existing DM')

    def send(self, status):
        try:
            result = self._call('--to', 'photon:' + self.target, '--json', '--file', '-',
                                payload=status.encode('ascii'))
        except (FileNotFoundError, PermissionError):
            return 'rejected'  # Executable missing or inaccessible.
        except (subprocess.TimeoutExpired, OSError):
            return 'ambiguous'  # Includes pipe errors after the child was launched.
        body = _mapping(result.stdout) or {}
        receipt = body.get('message_id')
        if (result.returncode == 0 and body.get('success') is True and not body.get('error')
                and not body.get('skipped') and not body.get('partial_success')
                and isinstance(receipt, str) and receipt.strip()):
            return 'accepted'
        if (result.returncode == 1 and body.get('error') and not body.get('success')
                and not receipt and not body.get('message_ids') and not body.get('partial_success')
                and (body.get('retryable') is True or body.get('error_class') == 'target_not_allowed')):
            return 'rejected'
        return 'ambiguous'


def _load_config(path):
    config = _mapping(path.read_bytes())
    fields = {'source_home', 'ledger', 'owner_keys', 'photon_home', 'photon_dm_id', 'hermes_executable'}
    if config is None or config.keys() != fields:
        raise ValueError('invalid private relay configuration')
    paths = {}
    for field in fields - {'owner_keys', 'photon_dm_id'}:
        value = config[field]
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError('private relay configuration requires absolute paths')
        paths[field] = Path(value).resolve()
    executable = paths['hermes_executable']
    if not executable.is_file() or (os.name == 'nt' and executable.suffix.lower() != '.exe'):
        raise ValueError('a native Hermes CLI executable is required')
    if not paths['photon_home'].is_dir():
        raise ValueError('invalid Photon profile home')
    photon = Photon(executable, paths['photon_home'], config['photon_dm_id'])
    # A home change can change the sidecar account even if the same ID is reachable.
    relay = Relay(paths['source_home'] / 'state.db', paths['ledger'], config['owner_keys'],
                  [str(paths['photon_home']), photon.target])
    return relay, photon


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'activate', 'poll', 'status'])
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    stage = 'loading private configuration'
    try:
        relay, photon = _load_config(args.config)
        if args.action == 'status':
            stage = 'reading relay ledger'
            print(json.dumps(relay.status(), sort_keys=True))
            return 0
        stage = 'verifying pinned Photon DM'
        photon.check_route()
        if args.action == 'check':
            stage = 'verifying Hermes source metadata'
            with relay._snapshot() as (_, _, _, ids):
                print(json.dumps({'verified_session_segments': len(ids), 'verified_photon_dm': True}))
        elif args.action == 'activate':
            stage = 'seeding the forward-only ledger'
            relay.activate()
            print('Relay baseline saved; no message sent.')
        else:
            stage = 'polling or recording completion delivery'
            outcomes = relay.poll(photon.send)
            if outcomes:
                print(json.dumps(outcomes, sort_keys=True))
            return 2 if outcomes.get('rejected') or outcomes.get('ambiguous') else 0
    except (ValueError, sqlite3.Error, OSError, subprocess.SubprocessError) as exc:
        # Provider errors, paths and route IDs may contain private values.
        print(f'Relay stopped while {stage} ({type(exc).__name__}).', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
