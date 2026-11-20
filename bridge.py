"""Private single-worker bridge. submit_verified is NOT a public ingress."""
import hashlib
import json
import re
from collections import Counter, deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from threading import Condition, Thread
from time import monotonic
from types import MappingProxyType
from typing import Mapping

SESSION_KEY = 'alexa:household:voice:v1'
TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted'}
FRESH_SECONDS = 150
ENTITY = re.compile(r'notify\.[a-z0-9_]{1,128}')


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[!-~]{1,512}', value) is not None


def fresh_timestamp(stamp):
    if not isinstance(stamp, str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})', stamp, re.ASCII):
        return False
    try:
        when = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        return abs((datetime.now(timezone.utc) - when).total_seconds()) <= FRESH_SECONDS
    except (ValueError, OverflowError):
        return False


@dataclass(frozen=True)
class Policy:
    skill_id: str
    routes: Mapping[tuple[str, str], str]
    device_defaults: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config):
        """Build from operator-owned routes JSON, not an Alexa envelope."""
        if not isinstance(config, dict) or set(config) != {'skill_id', 'routes'}:
            raise ValueError('invalid policy config')
        rows = config['routes']
        if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
            raise ValueError('invalid route count')
        routes, defaults = {}, {}
        required = {'user_id', 'device_id', 'entity_id'}
        for row in rows:
            if not isinstance(row, dict) or not required <= row.keys() <= required | {'default_conversation'}:
                raise ValueError('invalid route fields')
            user, device = row['user_id'], row['device_id']
            mode = row.get('default_conversation', 'house')
            if not all(map(identifier, (user, device))) or (user, device) in routes:
                raise ValueError('invalid or duplicate route identity')
            if defaults.setdefault(device, mode) != mode:
                raise ValueError('conflicting defaults for one Echo')
            routes[user, device] = row['entity_id']
        return cls(config['skill_id'], routes, defaults)

    def __post_init__(self):
        routes = dict(self.routes)
        if not identifier(self.skill_id) or not 1 <= len(routes) <= 64:
            raise ValueError('invalid skill or route count')
        by_device, by_entity = {}, {}
        for pair, entity in routes.items():
            if (not isinstance(pair, tuple) or len(pair) != 2 or not all(map(identifier, pair))
                    or not isinstance(entity, str) or not ENTITY.fullmatch(entity)):
                raise ValueError('invalid route')
            device = pair[1]
            if by_device.setdefault(device, entity) != entity or by_entity.setdefault(entity, device) != device:
                raise ValueError('each Echo must map to exactly one distinct entity')
        defaults = dict(self.device_defaults)
        if any(device not in by_device or mode not in ('house', 'echo') for device, mode in defaults.items()):
            raise ValueError('invalid device default conversation')
        object.__setattr__(self, 'routes', MappingProxyType(routes))
        object.__setattr__(self, 'device_defaults', MappingProxyType(defaults))

    def conversation_for(self, device):
        if self.device_defaults.get(device) == 'echo':
            digest = hashlib.sha256(json.dumps([self.skill_id, device]).encode()).hexdigest()
            return 'alexa:echo:voice:v1:' + digest
        return SESSION_KEY


@dataclass(frozen=True)
class Job:
    key: str
    question: str
    entity: str
    admitted: float
    conversation: str
    target: str | None
    order: int


@dataclass
class Run:
    job: Job
    deadline: float
    run_id: str | None = None
    next_poll: float = 0
    steers: list = field(default_factory=list)
    control_seen: bool = False


def paired_request(policy, event):
    system = event['context']['System']
    skill = system['application']['applicationId']
    user, device = system['user']['userId'], system['device']['deviceId']
    request = event['request']
    rid, stamp = request['requestId'], request['timestamp']
    if (event['version'] != '1.0' or skill != policy.skill_id
            or not all(map(identifier, (user, device, rid))) or not fresh_timestamp(stamp)):
        raise ValueError('invalid request scope or freshness')
    entity = policy.routes.get((user, device))
    if entity is None:
        raise ValueError('unpaired request')
    return request, skill, user, device, entity, rid, stamp


class Bridge:
    def __init__(self, policy, hermes, speak, *, max_pending=4, max_seen=256,
                 queue_max_age=120, clock=monotonic, poll_interval=0.25, run_timeout=600,
                 lane=None):
        if not 1 <= max_pending <= max_seen <= 4096 or not 1 <= queue_max_age <= 300:
            raise ValueError('invalid queue limits')
        if not 0 < poll_interval <= 10 or not 0 < run_timeout <= 3600:
            raise ValueError('invalid Runs timing limits')
        if lane not in (None, 'owner', 'guest'):
            raise ValueError('invalid profile lane')
        self.lane = lane
        self.policy, self.hermes, self.speak = policy, hermes, speak
        self.poll_interval, self.run_timeout = poll_interval, run_timeout
        self.max_pending, self.max_seen = max_pending, max_seen
        self.queue_max_age, self.clock = queue_max_age, clock
        self.cv = Condition()
        self.queue, self.seen, self.stats = deque(), {}, Counter()
        self.active = {}
        self.unresolved = set()
        self.outstanding = 0
        self.closed = False
        self.worker = Thread(target=self._work, name='alexa-runs', daemon=True)
        self.worker.start()

    def submit_verified(self, event):
        """Accept only from an authenticated adapter or in-process synthetic fixture."""
        try:
            request, skill, user, device, entity, rid, stamp = paired_request(self.policy, event)
            if request['type'] == 'LaunchRequest':
                return 'launch'
            if request['type'] == 'SessionEndedRequest':
                return 'ended'
            if request['type'] != 'IntentRequest' or request['intent']['name'] != 'AskHermesIntent':
                return 'unsupported'
            text = request['intent']['slots']['question']['value']
            if not isinstance(text, str) or not 1 <= len(text) <= 1000:
                return 'rejected'
            text.encode('utf-8')
            if not (text := ' '.join(text.split())) or any(ord(c) < 32 or ord(c) == 127 for c in text):
                return 'rejected'
        except (KeyError, TypeError, ValueError, OverflowError):
            return 'rejected'

        conversation = self.policy.conversation_for(device)
        key = self.request_key(skill, rid)
        if self.lane is not None:
            conversation = f'alexa:{self.lane}:' + conversation.removeprefix('alexa:')
        fingerprint = hashlib.sha256(json.dumps([user, device, stamp, text]).encode()).digest()
        with self.cv:
            if self.closed or not self.worker.is_alive():
                return 'busy'
            now = self.clock()
            # A timestamp may be 150s ahead at admission, then remain valid for another 150s.
            expired = [k for k, (_, until) in self.seen.items() if until <= now]
            for old in expired:
                del self.seen[old]
            if key in self.seen:
                return 'duplicate' if self.seen[key][0] == fingerprint else 'rejected'
            if (conversation in self.unresolved or self.outstanding >= self.max_pending
                    or len(self.seen) >= self.max_seen):
                return 'busy'
            run = self.active.get(conversation)
            target = run.run_id if run else None
            self.seen[key] = (fingerprint, now + 2 * FRESH_SECONDS + 5)
            self.queue.append(Job(key, text, entity, now, conversation, target, self.stats['accepted']))
            self.outstanding += 1
            self.stats['accepted'] += 1
            self.cv.notify_all()
        return 'steering' if target else 'accepted'

    def request_key(self, skill, rid):
        prefix = 'alexa-' if self.lane is None else f'alexa-{self.lane}-'
        return prefix + hashlib.sha256(json.dumps([skill, rid]).encode()).hexdigest()

    def has_admitted(self, skill, rid):
        with self.cv:
            entry = self.seen.get(self.request_key(skill, rid))
            return entry is not None and self.clock() < entry[1]

    def _work(self):
        # Only this worker performs network I/O. Admission never waits for it.
        while True:
            with self.cv:
                now = self.clock()
                for job in self.queue:
                    run = self.active.get(job.conversation)
                    if job.conversation in self.unresolved:
                        action = 'run_unresolved'
                    elif now - job.admitted >= self.queue_max_age:
                        action = 'expire'
                    elif run is None:
                        action = 'start'
                    elif job.target is not None and job.target == run.run_id:
                        action = 'steer'
                    else:
                        continue
                    self.queue.remove(job)
                    if action == 'start':
                        run = self.active[job.conversation] = Run(job, now + self.run_timeout)
                    break
                else:
                    run = min(self.active.values(), key=lambda item: item.next_poll, default=None)
                    if run is not None and run.next_poll <= now:
                        action = 'poll'
                    else:
                        if self.closed and not self.outstanding:
                            return
                        self.cv.wait(None if run is None else max(0, run.next_poll - now))
                        continue
            if action == 'start':
                self._start(run)
            elif action == 'steer':
                self._steer(run, job)
            elif action == 'poll':
                self._poll(run)
            else:
                self._settle('expired' if action == 'expire' else action)

    def _start(self, run):
        job = run.job
        try:
            run_id = self.hermes.start(job.question, job.key, job.conversation)
        except Exception:
            # Without a usable receipt we cannot prove the POST did not start a run.
            self._finish(run, 'run_create_unknown')
        else:
            with self.cv:
                run.run_id = run_id

    def _steer(self, run, job):
        try:
            accepted = self.hermes.steer(run.run_id, job.question)
        except Exception:
            # A lost or malformed reply is not proof that Hermes rejected the write.
            run.steers.append((job, False))
        else:
            if accepted:
                run.steers.append((job, True))
            else:
                # A refusal proves non-acceptance, not why it happened. A later
                # terminal status cannot distinguish completion from pause/stop.
                self._settle('steer_refused')
                run.next_poll = 0

    def _poll(self, run):
        try:
            status = self.hermes.status(run.run_id)
        except Exception:
            status = {}
        state = status.get('status')
        # Canonical control prompts can be pending even while status is 'running'.
        # A later poll can lose the prompt; don't use terminal replay to bypass it.
        controls = status.get('pending_controls', [])
        if controls != [] or state in {'waiting_for_approval', 'waiting_for_clarification', 'stopping'}:
            run.control_seen = True
        if state in TERMINAL:
            self._finish(run, self._deliver(run.job, status), status)
            return
        if self.clock() >= run.deadline:
            self._finish(run, 'run_timeout')
        else:
            run.next_poll = self.clock() + self.poll_interval

    @staticmethod
    def _pending_jobs(attempts, pending):
        """Map Hermes' newline-joined undelivered suffix back to original jobs.

        Transport-unknown writes might be absent. When repeated text makes a
        match uncertain, fail closed instead of guessing its Echo or replaying.
        """
        if pending is None or pending == '':
            return []
        if not isinstance(pending, str):
            return None
        lines = pending.split('\n')
        if len(lines) > len(attempts):
            return None
        all_text = Counter(job.question for job, _ in attempts)
        pending_text = Counter(lines)
        if any(not accepted and 0 < pending_text[job.question] < all_text[job.question]
               for job, accepted in attempts):
            return None
        matches = []
        for job, accepted in reversed(attempts):
            if job.question == lines[-1]:
                matches.append(job)
                lines.pop()
                if not lines:
                    return matches[::-1]
            elif accepted:
                break
        return None

    def _requeue(self, jobs):
        now = self.clock()
        self.queue.extend(replace(job, target=None, admitted=now) for job in jobs)
        self.queue = deque(sorted(self.queue, key=lambda job: job.order))

    def _finish(self, run, outcome, status=None):
        pending = None
        if status is not None and run.steers and not run.control_seen:
            try:
                text = status.get('pending_steer')
                # Canonical status can omit this field; the terminal event carries it.
                if 'pending_steer' not in status and status.get('admission_id'):
                    text = self.hermes.terminal_pending(run.run_id, status['status'])
                pending = self._pending_jobs(run.steers, text)
            except Exception:
                pass  # No trustworthy evidence for replay.
        replay_keys = {job.key for job in pending or []}
        with self.cv:
            if outcome in {'run_timeout', 'run_create_unknown'}:
                self.unresolved.add(run.job.conversation)
            del self.active[run.job.conversation]
            self._settle(outcome)
            for job, accepted in run.steers:
                if job.key not in replay_keys:
                    self._settle('steer_not_pending' if accepted and pending is not None else 'steer_unknown')
            if pending:
                self._requeue(pending)
                self.stats['steer_fallback'] += len(pending)

    def _settle(self, outcome):
        with self.cv:
            self.outstanding -= 1
            self.stats[outcome] += 1
            self.cv.notify_all()

    def _deliver(self, job, status):
        result = status.get('output')
        if status['status'] != 'completed' or not isinstance(result, str) or not result.strip():
            return 'hermes_failed'
        try:
            self.speak(job.entity, result)
        except Exception:
            return 'ha_failed_or_unknown'
        return 'ha_accepted'

    def counts(self):
        with self.cv:
            return dict(self.stats)

    def wait_idle(self, timeout):
        with self.cv:
            return self.cv.wait_for(lambda: not self.outstanding, timeout)

    def close(self, timeout=1):
        """Stop admitting; request drain. False if still draining at deadline."""
        with self.cv:
            self.closed = True
            self.cv.notify_all()
        self.worker.join(timeout)
        return not self.worker.is_alive()
