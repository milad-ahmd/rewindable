"""The approval code: a second secret, held only by a human, that approving needs.

Without it, anything that can reach the journal can answer an approval. That
includes an agent with a shell tool, which could propose a gated call and then run
``rewindable approve`` on it in its next step. With a code set, approving through
rewindable (``Engine.approve``, ``Engine.signal``, the CLI or the web UI) needs that
code. The journal keeps only its SHA-256. Rejecting needs no code: it never makes
anything run.

The check also runs where the answer is read. An approval that reaches the journal
without the code (a direct ``Store.deliver_signal``, say) is read as a rejection.

This is not a sandbox. Code running as your user can still edit the journal file
itself. Pair the code with tools that cannot touch the journal, for example a
sandbox or a separate OS user.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

SETTING = "approval_code"
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O or U to misread
PREFIX = "sha256:"


def new_code() -> str:
    """Twenty random characters (100 bits) in four groups: ``7K2QF-…``."""
    groups = ("".join(secrets.choice(ALPHABET) for _ in range(5)) for _ in range(4))
    return "-".join(groups)


def normalize(code: str) -> str:
    return "".join(code.split()).replace("-", "").upper()


def digest(code: str) -> str:
    return PREFIX + hashlib.sha256(normalize(code).encode()).hexdigest()


def matches(stored: str, code: str | None) -> bool:
    """Whether ``code`` is the one ``stored`` was made from. A stored value that is
    empty or malformed matches nothing, so a damaged setting refuses every approval
    instead of letting them all through."""
    if not code or not stored.startswith(PREFIX) or len(stored) != len(PREFIX) + 64:
        return False
    return hmac.compare_digest(stored, digest(code))


def approves(payload: Any) -> bool:
    """Whether a signal payload would read as "approved" to ``ctx.approve``."""
    if isinstance(payload, dict):
        return bool(payload.get("approved"))
    return bool(payload)
