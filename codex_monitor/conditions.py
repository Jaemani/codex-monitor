"""Durable change-debounce policy for sampled JSON values.

The policy deliberately does not own the last emitted value.  The caller passes
that authoritative value (normally the ``ChangeWatcher`` checkpoint) to
``select`` and calls ``commit`` only after downstream handling succeeds.  This
keeps a crash between selection and delivery replayable.  Stability means
matching samples observed by the sampler; it cannot establish continuity for
real-world changes that happen between samples.
"""

from __future__ import annotations

import json
import math
from numbers import Real
from pathlib import Path
import time

from .watch import atomic_json


STATE_VERSION = 1


class ConditionStateError(ValueError):
    """The durable condition state is missing required or valid data."""


class ConditionDebouncer:
    """Durably select samples that have remained stable for a time window.

    ``baseline`` is supplied by the caller because it is the source of truth
    for what has already been emitted.  ``select`` returns the sample when it
    is eligible and leaves a candidate checkpoint in place until ``commit``.
    A changed ``pause_epoch`` starts the candidate's observed-age window over.
    Callers should advance that epoch whenever sampling resumes after a pause.
    ``baseline=None`` means that no watcher checkpoint exists; a pending
    candidate alongside it is an inconsistent state and raises an error.

    A newly constructed policy instance re-anchors an existing candidate on
    its first observation.  This preserves the exact candidate while avoiding
    credit for stability observed by no running sampler during a restart.
    """

    def __init__(self, path, debounce_seconds=0, *, clock=time.monotonic):
        self.path = Path(path)
        self.debounce_seconds = self._duration(debounce_seconds)
        self.clock = clock
        self._observing = False

    @staticmethod
    def _duration(value):
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError("debounce_seconds must be a finite non-negative number")
        value = float(value)
        if not math.isfinite(value) or value < 0:
            raise ValueError("debounce_seconds must be a finite non-negative number")
        return value

    @staticmethod
    def _epoch(value):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("pause_epoch must be a non-negative integer")
        return value

    def _now(self):
        value = self.clock()
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError("clock must return a finite number")
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("clock must return a finite number")
        return value

    def _load(self):
        if not self.path.exists():
            return {"version": STATE_VERSION, "candidate": None}
        try:
            state = json.loads(self.path.read_text())
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConditionStateError(f"cannot read condition state {self.path}") from exc
        version = state.get("version") if isinstance(state, dict) else None
        if (isinstance(version, bool) or not isinstance(version, int)
                or version != STATE_VERSION):
            raise ConditionStateError(f"unsupported condition state in {self.path}")
        if "candidate" not in state:
            raise ConditionStateError(f"missing candidate in condition state {self.path}")
        candidate = state["candidate"]
        if candidate is None:
            return {"version": STATE_VERSION, "candidate": None}
        if not isinstance(candidate, dict) or "sample" not in candidate:
            raise ConditionStateError(f"invalid candidate in condition state {self.path}")
        first_seen = candidate.get("first_seen")
        if (isinstance(first_seen, bool) or not isinstance(first_seen, Real)
                or not math.isfinite(float(first_seen))):
            raise ConditionStateError(f"invalid candidate timestamp in condition state {self.path}")
        last_seen = candidate.get("last_seen")
        if (isinstance(last_seen, bool) or not isinstance(last_seen, Real)
                or not math.isfinite(float(last_seen))):
            raise ConditionStateError(f"invalid candidate last-seen timestamp in condition state {self.path}")
        pause_epoch = candidate.get("pause_epoch")
        if (isinstance(pause_epoch, bool) or not isinstance(pause_epoch, int)
                or pause_epoch < 0):
            raise ConditionStateError(f"invalid candidate pause epoch in condition state {self.path}")
        return {
            "version": STATE_VERSION,
            "candidate": {
                "sample": candidate["sample"],
                "first_seen": float(first_seen),
                "last_seen": float(last_seen),
                "pause_epoch": pause_epoch,
            },
        }

    def _save(self, state):
        # atomic_json also rejects NaN and non-JSON samples, so a candidate is
        # never checkpointed in a representation that cannot be read back.
        atomic_json(self.path, state)

    def select(self, sample, baseline, *, pause_epoch=0):
        """Return ``sample`` when eligible, or ``None`` while debouncing.

        The first sample (``baseline is None``) is returned immediately so the
        caller can hand it to ``ChangeWatcher`` to establish its silent
        baseline.  For a non-zero debounce, a changed sample is checkpointed
        before its stable-age window begins.  Eligibility does not clear that
        checkpoint; call ``commit`` after the downstream watcher succeeds.
        """
        pause_epoch = self._epoch(pause_epoch)
        state = self._load()
        candidate = state["candidate"]

        if baseline is None:
            if candidate is not None:
                raise ConditionStateError(
                    f"baseline is missing while a candidate is pending in {self.path}"
                )
            if not self.path.exists():
                self._save(state)
            self._observing = True
            return sample

        first_observation = not self._observing
        self._observing = True

        if sample == baseline:
            if candidate is not None:
                state["candidate"] = None
                self._save(state)
            return None

        now = self._now()
        if candidate is None or candidate["sample"] != sample:
            candidate = {
                "sample": sample,
                "first_seen": now,
                "last_seen": now,
                "pause_epoch": pause_epoch,
            }
            state["candidate"] = candidate
            self._save(state)
            return sample if self.debounce_seconds == 0 else None

        if (first_observation or candidate["pause_epoch"] != pause_epoch
                or now < candidate["last_seen"]):
            # A pause means no observations were made; a wall-clock rollback
            # or a restart similarly invalidates age measured in the old clock
            # epoch. The candidate itself remains available for replay.
            candidate["first_seen"] = candidate["last_seen"] = now
            candidate["pause_epoch"] = pause_epoch
            self._save(state)
            return sample if self.debounce_seconds == 0 else None

        if now != candidate["last_seen"]:
            candidate["last_seen"] = now
            self._save(state)
        if now - candidate["first_seen"] >= self.debounce_seconds:
            return sample
        return None

    def status(self):
        """Return the pending candidate without changing durable state."""
        candidate = self._load()["candidate"]
        return {"pending": candidate is not None, "candidate": candidate}

    def commit(self, sample):
        """Clear an eligible candidate after downstream handling succeeds."""
        state = self._load()
        candidate = state["candidate"]
        if candidate is None:
            return False
        if candidate["sample"] != sample:
            raise ValueError("committed sample does not match condition candidate")
        state["candidate"] = None
        self._save(state)
        return True

    def reset(self):
        """Discard a candidate, explicitly ending its observed stable window."""
        state = self._load()
        if state["candidate"] is None:
            return False
        state["candidate"] = None
        self._save(state)
        return True
