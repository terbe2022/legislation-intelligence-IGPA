"""Bill identification, hashing, and status helpers.

Pure functions only — no I/O, no side effects. Easy to unit-test.

Notes on terminal status:
  LegiScan returns a numeric `status` and a human `status_desc`. Different
  states use different codes; for IL, the codes we care about are:
    1 = Introduced
    2 = Engrossed
    3 = Enrolled
    4 = Passed (signed by governor)
    5 = Vetoed
    6 = Failed/Died
  We treat 4, 5, 6 as terminal by default. This is overridable in config so
  we can tighten or loosen the definition without code changes.
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional


# ---------------------------------------------------------------- chamber
def chamber_from_bill_number(bill_number: str) -> Optional[str]:
    """Infer chamber from the bill_number prefix.

    HB / HJR / HJRCA / HR / HLP -> House
    SB / SJR / SJRCA / SR / SLP -> Senate

    Returns None for unrecognized prefixes (don't crash on unknown types).
    """
    if not isinstance(bill_number, str):
        return None
    s = bill_number.strip().upper()
    if s.startswith("H"):
        return "House"
    if s.startswith("S"):
        return "Senate"
    return None


# ------------------------------------------------------------------- hash
def content_hash(text: str) -> str:
    """SHA256 of the bill's full_text. Used to detect amendments.

    Empty / None text returns the hash of the empty string — a deterministic
    sentinel so two missing-text bills compare equal.
    """
    if not text:
        text = ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -------------------------------------------------------- bill_number parse
_BILLNUM_RE = re.compile(r"^\s*(?P<type>[A-Z]+)\s*0*(?P<num>\d+)\s*$")


def parse_bill_number(bill_number: str) -> tuple[Optional[str], Optional[int]]:
    """Parse 'HB1234' / 'HB 0001' / 'hb-1' into ('HB', 1234).

    Returns (None, None) if it doesn't look like a bill number — caller
    decides whether that's a bug worth raising or just data to skip.
    """
    if bill_number is None:
        return None, None
    s = str(bill_number).upper().replace("-", " ").strip()
    s = re.sub(r"\s+", " ", s).replace(" ", "")
    m = _BILLNUM_RE.match(s)
    if not m:
        return None, None
    return m.group("type"), int(m.group("num"))


def normalize_bill_number(bill_number: str) -> Optional[str]:
    """Canonical form for use as a filename: 'HB0001'.

    Always 4 digits, zero-padded. Returns None if unparseable.
    """
    bt, num = parse_bill_number(bill_number)
    if bt is None or num is None:
        return None
    return f"{bt}{num:04d}"


# ---------------------------------------------------------- terminal status
DEFAULT_TERMINAL_STATUS_CODES = frozenset({4, 5, 6})


def is_terminal_status(
    status_code: Optional[int],
    terminal_codes: Optional[frozenset] = None,
) -> bool:
    """Whether a bill is in a state where we should stop checking for updates.

    Configurable via terminal_codes; defaults to {4 (Passed), 5 (Vetoed),
    6 (Failed/Died)}.
    """
    if status_code is None:
        return False
    codes = terminal_codes or DEFAULT_TERMINAL_STATUS_CODES
    try:
        return int(status_code) in codes
    except (TypeError, ValueError):
        return False
