"""
stage2_transformers.py
======================

Transformers used inside the Stage 2 pipeline. Kept in their own module so the
pickled model references `stage2_transformers.X` rather than `__main__.X`, and
any script run from the Legislation_Pipeline directory can unpickle it without a
shim.

Keep this file next to the model. If it moves, the model stops loading.
"""

import re
import scipy.sparse as sp
from sklearn.base import BaseEstimator, TransformerMixin

_SEP = " || "
_SEGMENTS_PER_ACTION = 4   # clean_action || semantic || procedural || activity


class SparseToDense(BaseEstimator, TransformerMixin):
    """Identical to the class used by the deployed v2/v3 models."""
    def fit(self, X, y=None):
        return self

    def transform(self, X):
        return X.toarray() if sp.issparse(X) else X


class DepthTokens(BaseEstimator, TransformerMixin):
    """
    Append depth-awareness tokens to each prefix text.

    The model is a bag of words. Without this, "Referred to Rules" at action 2
    (routine - nearly every bill) and "Re-referred to Rules" at action 15 (death)
    are indistinguishable from position alone. Appending tokens that encode how
    many actions have been seen lets a tree model learn depth-conditional
    patterns: the same phrase can mean different things at different depths.

    Depth is derived from the text itself (segment count), so the interface is
    unchanged: strings in, strings out. Nothing upstream has to pass depth.

    Tokens emitted:
        depthtok_<k>        exact depth, capped at 40
        depthgte_<n>        cumulative flags for n in THRESHOLDS, so a single
                            split on "depthgte_5" separates early from late
    """
    THRESHOLDS = (2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30)
    MAX_DEPTH = 40

    def fit(self, X, y=None):
        return self

    @classmethod
    def depth_of(cls, text):
        if not text:
            return 1
        n_seg = text.count(_SEP) + 1
        return max(1, min(cls.MAX_DEPTH, round(n_seg / _SEGMENTS_PER_ACTION)))

    @classmethod
    def tokens_for(cls, depth):
        toks = [f"depthtok_{depth}"]
        toks += [f"depthgte_{n}" for n in cls.THRESHOLDS if depth >= n]
        return " ".join(toks)

    def transform(self, X):
        out = []
        for t in X:
            t = t if isinstance(t, str) else ""
            out.append(t + " " + self.tokens_for(self.depth_of(t)))
        return out


# ── v5: bill-text prefix ──────────────────────────────────────────────────────
TITLE_MAX = 200
SYNOPSIS_MAX = 400


def build_stage2_text(actions_text, title="", description=""):
    """
    Assemble the text the v5 model expects.

        "<title> || <synopsis> || <action prefix as before>"

    The title block is constant for a bill, so at depth 1 it is the only thing
    that varies between bills. It is known at introduction and never leaks the
    outcome. Every caller that scores the v5 model - training, the trail rebuild,
    run_daily.py - must build text with this function so the format matches.

    actions_text may be "" (bill-only, used when precomputing the head).
    """
    title = (title or "").strip()[:TITLE_MAX]
    desc = (description or "").strip()[:SYNOPSIS_MAX]
    head = _SEP.join(x for x in (title, desc) if x)
    if not actions_text:
        return head
    return head + _SEP + actions_text if head else actions_text


# ══════════════════════════════════════════════════════════════════════════════
# SHARED INPUT FORMAT
# ══════════════════════════════════════════════════════════════════════════════
#
# Every model since June was trained on text built one way: each non-leakage
# action as  "<action, vote counts stripped> || <semantic> || <procedural> ||
# <activity>", those rows joined with " || ", capped at 40 actions. Before this
# module, nightly scoring (run_daily.py) and the trail rebuild built text a
# different way - all actions joined, then all semantic labels joined - so models
# were scored on input shaped unlike anything they were trained on.
#
# Training, the trail rebuild and nightly scoring must all build text with the
# functions below. That is the whole point of putting them here.

import pandas as pd

LEAKAGE_STAGES = {
    "Passed / Enrolled", "Failed / Rejected",
    "Governor Certification / Return", "Public Act / Effective Date",
}
LEAKAGE_TERMS = [
    r"\bpassed\b", r"\bfailed\b", r"\bvetoed?\b", r"\badopted\b",
    r"\benrolled\b", r"\boverride\b", r"\bwithdrawn\b",
    r"governor approved", r"public act", r"session sine die", r"sent to the governor",
]
_LEAK = re.compile("|".join(LEAKAGE_TERMS), re.IGNORECASE)
_VOTE_COUNT = re.compile(r"\b\d{3}-\d{3}-\d{3}\b")
MAX_PREFIX_DEPTH = 40


def head_text(title, description):
    """Title/synopsis head exactly as retrain_stage2_v5.py builds it."""
    return build_stage2_text("", title, description).rstrip(" |")


def model_needs_title(bundle):
    """True if the saved bundle's metadata says it was trained with a title head."""
    meta = bundle.get("meta") if isinstance(bundle, dict) else None
    return "build_stage2_text" in str((meta or {}).get("input_format", ""))


def prepare_actions(hist):
    """
    Sort, flag leakage and build per-action row text, identically to training.

    hist needs: bill_id, date, sequence, action, semantic_stage,
                procedural_stage, activity_type
    """
    h = hist.copy()
    h["date"] = pd.to_datetime(h["date"], errors="coerce")
    h["sequence"] = pd.to_numeric(h["sequence"], errors="coerce").fillna(0).astype(int)
    h = h.sort_values(["bill_id", "date", "sequence"], kind="stable")
    act = h["action"].fillna("")
    h["is_leak"] = h["semantic_stage"].isin(LEAKAGE_STAGES) | act.str.contains(_LEAK, regex=True)
    clean = act.str.replace(_VOTE_COUNT, "", regex=True).str.strip()
    h["row_text"] = (clean + _SEP + h["semantic_stage"].fillna("") + _SEP
                     + h["procedural_stage"].fillna("") + _SEP
                     + h["activity_type"].fillna(""))
    return h


def bill_prefix_texts(g, head=None):
    """
    One scoring text per action row of a single bill, in order.

    Non-leakage actions extend the prefix; leakage actions carry the previous
    prefix forward, so every displayed action gets a score but no score is ever
    computed from outcome text. Stops extending after MAX_PREFIX_DEPTH non-leakage
    actions, exactly as training capped depth. Returns a list aligned with g's
    rows; entries are None for leakage rows that occur before any real action.
    """
    out, acc, depth = [], None, 0
    for is_leak, row in zip(g["is_leak"].tolist(), g["row_text"].tolist()):
        if not is_leak and depth < MAX_PREFIX_DEPTH:
            acc = row if acc is None else acc + _SEP + row
            depth += 1
        if acc is None:
            out.append(None)
        else:
            out.append((head + _SEP + acc) if head is not None else acc)
    return out


def full_history_text(g, head=None):
    """Text for a bill's full history - what nightly scoring should use."""
    texts = [t for t in bill_prefix_texts(g, head) if t is not None]
    return texts[-1] if texts else None
