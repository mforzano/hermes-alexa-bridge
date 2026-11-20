"""Opt-in split routing, behind the existing signed Alexa ingress."""
import os
import re
import stat
from threading import Lock
from time import monotonic

from bridge import Bridge, paired_request
from voice_identity import matches_voice, registration_digest, voice_digest


def read_fingerprint(path):
    """Read the existing enrollment format without bringing an ID into config."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or getattr(before, 'st_file_attributes', 0) & 0x400:
        raise ValueError('enrollment must be a private regular file')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
                 | getattr(os, 'O_BINARY', 0))
    with os.fdopen(fd, 'rb') as stream:
        current = os.fstat(stream.fileno())
        if (not stat.S_ISREG(current.st_mode) or not os.path.samestat(before, current)
                or (os.name == 'posix' and current.st_mode & 0o077)):
            raise ValueError('enrollment must be a private regular file')
        raw = stream.read(133)
    if not re.fullmatch(rb'[a-f0-9]{64}(?:\r?\n[a-f0-9]{64})?(?:\r?\n)?', raw):
        raise ValueError('invalid enrollment fingerprint')
    fields = raw.decode('ascii').splitlines()
    return fields[0], fields[1] if len(fields) == 2 else None


class VoiceRouter:
    """One immutable route choice per admission; no cross-profile fallback."""
    def __init__(self, policy, guest, owner, speak, fingerprint, enrollment=None, enrolled_request=None):
        self.policy, self.fingerprint = policy, fingerprint
        self.enrollment, self.enrolled_request = enrollment, enrolled_request
        self.guest = Bridge(policy, guest, speak, lane='guest')
        self.owner = Bridge(policy, owner, speak, lane='owner')
        self.lock = Lock()
        self.closed = False

    def submit_verified(self, event):
        try:
            _, skill, _, _, _, rid, _ = paired_request(self.policy, event)
        except (KeyError, TypeError, ValueError, OverflowError):
            return 'rejected'
        with self.lock:
            if self.closed:
                return 'busy'
            if self.enrolled_request == registration_digest(rid):
                return 'duplicate'
            if (self.enrollment is not None and not self.guest.has_admitted(skill, rid)
                    and not self.owner.has_admitted(skill, rid)):
                try:
                    outcome = self.enrollment(event)
                    if outcome == 'voice_enrolled':
                        self.enrollment.expiry = 0  # Permanently disarm writes in this process.
                        self.enrolled_request = registration_digest(rid)
                        fingerprint, consumed = read_fingerprint(self.enrollment.path)
                        if fingerprint != voice_digest(event) or consumed != self.enrolled_request:
                            return 'voice_enrollment_unavailable'
                        self.fingerprint = fingerprint
                except (OSError, ValueError):
                    return 'voice_enrollment_unavailable'
                if outcome is not None:
                    return outcome
            if self.fingerprint is not None and matches_voice(event, self.fingerprint):
                selected, other = self.owner, self.guest
            else:
                selected, other = self.guest, self.owner
            if other.has_admitted(skill, rid):
                return 'rejected'
            return selected.submit_verified(event)

    def wait_idle(self, timeout):
        deadline = monotonic() + timeout
        return self.guest.wait_idle(timeout) and self.owner.wait_idle(max(0, deadline - monotonic()))

    def close(self, timeout=1):
        deadline = monotonic() + timeout
        with self.lock:
            self.closed = True
            for bridge in (self.guest, self.owner):
                bridge.close(0)
        guest_closed = self.guest.close(max(0, deadline - monotonic()))
        owner_closed = self.owner.close(max(0, deadline - monotonic()))
        return guest_closed and owner_closed
