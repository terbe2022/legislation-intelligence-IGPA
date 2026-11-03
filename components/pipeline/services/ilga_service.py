"""Illinois General Assembly (ILGA) full-text scraper.

Single source of truth: ILGA FTP directory.

  https://www.ilga.gov/ftp/legislation/{ga}/{bill_type}/{filename}.htm

Why FTP and not BillStatus:
  Earlier versions of this module tried the ILGA BillStatus FullText page
  first (https://www.ilga.gov/Legislation/BillStatus/FullText?...) because
  that's what LegiScan's `state_link` query parameters point to. The
  problem is BillStatus only renders the SYNOPSIS (the abstract), not the
  full statutory text. We measured: BillStatus returns ~2,800 chars for a
  typical HB; the FTP HTML for the same bill is ~23,000 chars. We were
  silently capturing only ~12% of bill content.
  Direct FTP HTML files contain the full text inside <code> blocks (or
  <pre>, depending on age of the bill). FTP-first is the correct primary.

Strategy (per bill):
  1. Build the canonical FTP filename:
       {ga}00{prefix}{num4}.htm   (e.g. 10400HB0001.htm)
       prefix differs for some types (HJR -> HJ, SJR -> SJ, etc.)
     Try GET'ing it directly. If 200, parse and return.
  2. If 404 or unreachable, fall back to listing the bill-type directory
     and matching by canonical name OR padded number OR raw bill number.
     Pick the most-recently-modified match (handles re-engrossed bills,
     amendments published as new files).
  3. Extract text from the HTML in this order: <code> blocks (joined),
     <pre>, then page body. The first one that yields >200 chars wins.

Polite-scraping discipline:
  - Custom User-Agent identifying the request as a browser-like client
  - Random sleep jitter between requests (0.3-0.7s)
  - Bounded retries with exponential-ish backoff
  - Treat 404 as "not present", not as an error to retry

Function-name contract preserved for stages/ingestion.py:
  - to_fulltext_url_from_state_link  : returns the FTP URL we'll try
                                       (legacy name kept; previously
                                       returned a BillStatus URL)
  - fetch_full_text                  : per-bill fetch, returns text or None
  - get_illinois_general_assembly_number : computes current GA from date
  - is_missing_full_text_series      : DataFrame helper
  - fill_missing_full_text_from_ilga_ftp : second-pass patcher for any
                                           bills that failed in pass one
"""

from __future__ import annotations

import html
import logging
import random
import re
import time
import unicodedata
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


# ============================================================================
# Constants and lookups
# ============================================================================

# Bill types we know how to find on ILGA FTP. Anything outside this list
# we don't even try (avoids wasting requests on resolutions that LegiScan
# tracks but ILGA doesn't expose at predictable paths).
BILL_TYPES = ["HB", "HJR", "HJRCA", "HR", "SB", "SJR", "SJRCA", "SLP", "SR"]

# ILGA's FTP filename uses different prefixes than the bill_number for some
# joint resolution types. Without this map, HJR0001 generates the wrong
# filename (HJR... instead of HJ...). The intern hit this; we won't.
FTP_FILE_PREFIX = {
    "HJR": "HJ",
    "SJR": "SJ",
    "HJRCA": "HC",
    "SJRCA": "SC",
}

# String values that mean "we never got real text". Used by
# is_missing_full_text_series and the FTP patch pass.
MISSING_SENTINELS = {"", "not found", "invalid url", "error: http 404", "none", "nan"}

# Browser-like User-Agent. ILGA serves identical content to scripted clients
# but some of their connection-pool behavior treats "python-requests/x.y.z"
# weirdly. Sending a real-looking UA avoids that class of issue.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)

# Module-level requests.Session — connection pool reuse + persistent UA.
# Each call to fetch_full_text() reuses the same TCP connection to ILGA
# instead of redoing TLS for every bill.
_SESSION: Optional[requests.Session] = None


def _session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.headers.update({"User-Agent": _UA})
    return _SESSION


# ============================================================================
# Text extraction & normalization
# ============================================================================

def _normalize_text(text: str) -> str:
    """Clean up scraped HTML text into something a tokenizer won't hate.

    - Decode HTML entities (&amp; etc.)
    - NFKC unicode normalization (collapses fullwidth/halfwidth variants)
    - Replace non-breaking spaces with regular spaces
    - Collapse 3+ blank lines into 2
    - Trim trailing whitespace per line
    """
    if not isinstance(text, str):
        return text
    text = html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_bill_text_from_html(html_text: str) -> Optional[str]:
    """Pull the bill body out of ILGA's FTP HTML.

    Tries three sources in order, returning the first that yields >200
    chars after normalization (the 200-char floor screens out empty
    pages and "Bill Not Found" placeholder responses).

    1. <code> blocks (joined with newlines) — the modern ILGA format
    2. <pre> block — older bills sometimes use this
    3. Whole body text — last resort

    Returns None if even the body fallback yields <200 chars (treats
    that as "no real content").
    """
    soup = BeautifulSoup(html_text, "html.parser")

    code_nodes = soup.find_all("code")
    if code_nodes:
        joined = "\n".join(c.get_text(separator="\n", strip=False) for c in code_nodes)
        cleaned = _normalize_text(joined)
        if len(cleaned) > 200:
            return cleaned

    pre = soup.find("pre")
    if pre:
        cleaned = _normalize_text(pre.get_text(separator="\n", strip=False))
        if len(cleaned) > 200:
            return cleaned

    body = (
        soup.body.get_text("\n", strip=False)
        if soup.body
        else soup.get_text("\n", strip=False)
    )
    cleaned = _normalize_text(body)
    if len(cleaned) > 200:
        return cleaned
    return None


# ============================================================================
# HTTP wrapper
# ============================================================================

def _request_with_retries(
    url: str,
    *,
    timeout: int = 30,
    retries: int = 3,
    sleep_base: float = 1.0,
) -> Optional[requests.Response]:
    """GET with bounded retries.

    - 404 returns None immediately (no point retrying a missing file).
    - Other errors retry up to `retries` times with sleep_base * attempt
      seconds plus random jitter.
    - Returns the Response on success, None after all retries exhausted.
    """
    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            r = _session().get(url, timeout=timeout)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r
        except Exception as e:
            last_err = e
            time.sleep(sleep_base * attempt + random.random())
    if last_err:
        logger.debug("Request failed after %d retries: %s (%s)", retries, url, last_err)
    return None


# ============================================================================
# GA computation (Illinois General Assembly number, e.g. 104)
# ============================================================================

def get_illinois_general_assembly_number(dt: Optional[datetime] = None) -> int:
    """Compute the GA number from a date.

    Anchored at GA 103 starting in 2023. Each GA is 2 years.
    2023-2024 = 103, 2025-2026 = 104, 2027-2028 = 105, etc.
    """
    if dt is None:
        dt = datetime.utcnow()
    base_year = 2023
    base_ga = 103
    return base_ga + ((dt.year - base_year) // 2)


# ============================================================================
# FTP URL building (canonical filename + directory listing fallback)
# ============================================================================

def _parse_bill_number(bill_number: Optional[str]) -> Optional[tuple[str, int]]:
    """Split a bill_number like 'HB0001' into ('HB', 1). Returns None if
    the format isn't recognized (so callers can skip without crashing).
    """
    if not isinstance(bill_number, str):
        return None
    m = re.match(r"^([A-Z]+)\s*0*([0-9]+)$", bill_number.strip().upper())
    if not m:
        return None
    bill_type = m.group(1)
    if bill_type not in BILL_TYPES:
        return None
    return bill_type, int(m.group(2))


def _build_direct_ftp_url(ga: int, bill_number: str) -> Optional[str]:
    """Build the canonical filename and full URL for a bill.

    Pattern:  https://www.ilga.gov/ftp/legislation/{ga}/{TYPE}/{ga}00{prefix}{num4}.htm

    For most types prefix == TYPE; for HJR/SJR/HJRCA/SJRCA it differs
    (see FTP_FILE_PREFIX). Pads the number to 4 digits.
    """
    parsed = _parse_bill_number(bill_number)
    if not parsed:
        return None
    bill_type, num_int = parsed
    num4 = str(num_int).zfill(4)
    prefix = FTP_FILE_PREFIX.get(bill_type, bill_type)
    filename = f"{ga}00{prefix}{num4}.htm"
    return f"https://www.ilga.gov/ftp/legislation/{ga}/{bill_type}/{filename}"


def _ftp_dir_list(base_url: str, *, timeout: int = 30) -> list[dict]:
    """Parse the ILGA FTP directory listing for a bill type.

    The page renders as a <pre> block of `MM/DD/YYYY  HH:MM AM  <bytes>  filename.htm`
    rows. Returns a list of {ftp_url, htm_file, last_modified} dicts —
    callers pick a match.
    """
    r = _request_with_retries(base_url, timeout=timeout)
    if not r:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    pre = soup.find("pre")
    if not pre:
        return []
    raw = pre.get_text()

    pattern = re.compile(
        r"(?P<date>\d{1,2}/\d{1,2}/\d{4})\s+"
        r"(?P<time>\d{1,2}:\d{2}\s+[AP]M)\s+"
        r"(?:<dir>|\d+)\s+"
        r"(?P<file>\S+\.htm)",
        re.IGNORECASE,
    )

    rows: list[dict] = []
    for m in pattern.finditer(raw):
        try:
            last_modified = datetime.strptime(
                f"{m.group('date')} {m.group('time')}", "%m/%d/%Y %I:%M %p"
            )
        except Exception:
            last_modified = None
        htm = m.group("file")
        rows.append({
            "ftp_url": urljoin(base_url, htm),
            "htm_file": htm,
            "last_modified": last_modified,
        })
    return rows


def _choose_best_ftp_url(
    ga: int, bill_number: str, *, timeout: int = 30, retries: int = 3,
) -> tuple[Optional[str], str]:
    """Find the best FTP URL for a bill.

    Returns (url, note) where note is one of:
      - "direct"           : canonical filename returned 200
      - "dir_latest"       : canonical missed; matched in directory listing
      - "bill_number_unparseable"
      - "unknown_bill_type"
      - "dir_empty_or_unreachable"
      - "no_dir_match"     : directory was readable but no filename matched

    The intent of returning the note is so the caller (and the manifest)
    can record _how_ a bill was resolved — useful when debugging FTP
    behavior changes later.
    """
    parsed = _parse_bill_number(bill_number)
    if not parsed:
        return None, "bill_number_unparseable"
    bill_type, num_int = parsed

    # Strategy 1: direct canonical URL
    direct = _build_direct_ftp_url(ga, bill_number)
    if direct:
        r = _request_with_retries(direct, timeout=timeout, retries=retries)
        if r:
            return direct, "direct"

    # Strategy 2: directory listing fallback
    base_url = f"https://www.ilga.gov/ftp/legislation/{ga}/{bill_type}/"
    rows = _ftp_dir_list(base_url, timeout=timeout)
    if not rows:
        return None, "dir_empty_or_unreachable"

    num_str = str(num_int)
    num4 = str(num_int).zfill(4)
    prefix = FTP_FILE_PREFIX.get(bill_type, bill_type)

    # Filename substrings to look for. Both unpadded and padded forms,
    # under both the bill_type prefix and the FTP-specific prefix.
    candidates = {
        f"{bill_type}{num_str}",
        f"{bill_type}{num4}",
        f"{prefix}{num_str}",
        f"{prefix}{num4}",
        bill_number.upper(),
    }

    matches = [row for row in rows if any(k in row["htm_file"].upper() for k in candidates)]
    if not matches:
        return None, "no_dir_match"

    # Most-recently-modified wins. Re-engrossed and amended bills get
    # newer files; we want the latest.
    matches.sort(key=lambda x: x["last_modified"] or datetime.min, reverse=True)
    return matches[0]["ftp_url"], "dir_latest"


# ============================================================================
# Public API — what stages/ingestion.py imports
# ============================================================================

def to_fulltext_url_from_state_link(state_link: str) -> Optional[str]:
    """Compatibility shim: returns the FTP URL the new pipeline uses.

    Previously this returned a BillStatus URL built from LegiScan's
    state_link. It now returns the canonical FTP URL for the bill
    referenced in state_link, derived from the bill_number portion.

    state_link looks like:
      https://www.ilga.gov/Legislation/BillStatus?DocNum=1&GAID=18&DocTypeID=HB&SessionID=114&GA=104

    We pull DocNum, DocTypeID, and GA out and construct the canonical
    FTP URL the same way _build_direct_ftp_url would.

    NOTE: stages/ingestion.py's `_resolve_full_text` records this on
    the manifest as `full_text_url`. After this change, that column
    will hold the FTP URL — semantically more accurate to what the
    pipeline actually used.
    """
    if not state_link:
        return None
    try:
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(state_link)
        q = parse_qs(parsed.query)
        # parse_qs is case-sensitive on keys; ILGA links are usually
        # consistent on capitalization but be tolerant.
        def get(key: str) -> Optional[str]:
            for k, v in q.items():
                if k.lower() == key.lower():
                    return v[0] if v else None
            return None

        doc_num = get("DocNum")
        doc_type = get("DocTypeID")
        ga = get("GA")
        if not (doc_num and doc_type and ga):
            return None
        try:
            num_int = int(doc_num)
            ga_int = int(ga)
        except (TypeError, ValueError):
            return None
        bill_type = doc_type.upper().strip()
        if bill_type not in BILL_TYPES:
            return None
        num4 = str(num_int).zfill(4)
        prefix = FTP_FILE_PREFIX.get(bill_type, bill_type)
        filename = f"{ga_int}00{prefix}{num4}.htm"
        return f"https://www.ilga.gov/ftp/legislation/{ga_int}/{bill_type}/{filename}"
    except Exception as e:
        logger.debug("Failed to parse state_link %r: %s", state_link, e)
        return None


def fetch_full_text(
    http_session,                    # accepted for back-compat; unused
    url: Optional[str],              # the canonical FTP URL we expect
    *,
    timeout: int = 30,
    retries: int = 3,
    min_sleep: float = 0.30,
    max_sleep: float = 0.70,
    bill_number: Optional[str] = None,  # used to derive a fallback URL
    ga: Optional[int] = None,
) -> Optional[str]:
    """Fetch a bill's full text via FTP.

    Hits the URL directly. If that fails (404 or no real content), and
    bill_number+ga were passed, falls back to the directory-listing
    matcher. Returns the cleaned full text or None.

    The polite-scraping sleep happens BEFORE the request so callers
    that loop over many bill numbers automatically get rate-limited.
    """
    # Polite-scraping sleep on entry
    time.sleep(min_sleep + random.random() * max(0.0, max_sleep - min_sleep))

    # Try the URL we were given first (state_link-derived canonical)
    if url:
        r = _request_with_retries(url, timeout=timeout, retries=retries)
        if r is not None:
            extracted = _extract_bill_text_from_html(r.text)
            if extracted:
                return extracted

    # Fallback: use bill_number + ga to find a match in the dir listing
    if bill_number and ga is not None:
        chosen, note = _choose_best_ftp_url(ga, bill_number, timeout=timeout, retries=retries)
        if chosen and (chosen != url):
            r = _request_with_retries(chosen, timeout=timeout, retries=retries)
            if r is not None:
                extracted = _extract_bill_text_from_html(r.text)
                if extracted:
                    logger.debug("Fetched %s via %s (note=%s)", bill_number, chosen, note)
                    return extracted

    return None


def is_missing_full_text_series(s: pd.Series) -> pd.Series:
    """Boolean Series — True where the value is missing/sentinel/error."""
    def _is_missing(v) -> bool:
        if v is None:
            return True
        try:
            if pd.isna(v):
                return True
        except (TypeError, ValueError):
            pass
        s_str = str(v).strip().lower()
        if s_str in MISSING_SENTINELS:
            return True
        if s_str.startswith("error"):
            return True
        return False
    return s.apply(_is_missing)


def fill_missing_full_text_from_ilga_ftp(
    df: pd.DataFrame,
    *,
    ga: int,
    timeout_seconds: int = 30,
    sleep_seconds: float = 0.5,
    min_text_len: int = 500,
    pick: str = "newest",       # accepted for back-compat; we always pick newest
    session=None,               # accepted for back-compat; unused
) -> pd.DataFrame:
    """Second-pass patcher: re-try any rows whose full_text is missing.

    Now that fetch_full_text already tries both the canonical URL and the
    directory listing on the first pass, this second pass is mostly a
    safety net — it covers transient network failures, since we re-issue
    the same logic with fresh requests.

    Iterates over rows where is_missing_full_text_series returns True.
    For each, runs _choose_best_ftp_url then a fresh fetch. Updates
    full_text and full_text_url in place.

    Args:
        min_text_len: ignored except for hard-floor check (any extracted
            text shorter than this is treated as "still missing").
    """
    if df.empty:
        return df

    out = df.copy()
    if "full_text" not in out.columns:
        out["full_text"] = None
    if "full_text_url" not in out.columns:
        out["full_text_url"] = None

    mask = is_missing_full_text_series(out["full_text"])
    to_fix_idx = out.index[mask].tolist()
    if not to_fix_idx:
        return out

    for k, idx in enumerate(to_fix_idx, start=1):
        bn = str(out.at[idx, "bill_number"])
        url, _note = _choose_best_ftp_url(ga, bn, timeout=timeout_seconds)
        if url:
            r = _request_with_retries(url, timeout=timeout_seconds)
            if r is not None:
                text = _extract_bill_text_from_html(r.text)
                if text and len(text) >= min_text_len:
                    out.at[idx, "full_text"] = text
                    out.at[idx, "full_text_url"] = url
        time.sleep(sleep_seconds + random.random() * 0.3)

    return out
