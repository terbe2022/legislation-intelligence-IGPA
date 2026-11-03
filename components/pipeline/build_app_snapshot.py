'[Deployment-specific example removed from this source docstring.]'
import sys, logging
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger(__name__)

sys.path.insert(0, '.')
from data_catalog import GOLD_URLS, PREDICTION_URLS, MANIFEST_URLS, TRAIL_URL
from services.azure_blob_service import BlobClient, TIER_GOLD

# ── Configuration ─────────────────────────────────────────────────────────────
ACTIVE_SESSION = 2176   # current session — the one the app shows by default

# Output blob paths (inside the gold container)
SNAP_PATH  = 'legislation/app_snapshots/active_bills_snapshot.parquet'
TRAIL_PATH = 'legislation/app_snapshots/active_bills_trail.parquet'

# Only keep columns the app actually uses — keeps the files small
GOLD_KEEP = [
    'bill_id', 'bill_number', 'title', 'status_date',
    'category_main_label', 'policy_domain_standardized',
    'beneficiary_primary_category', 'impact_rating_score',
    'llama_summary_raw', 'potential_impact_raw', 'impact_rating_clean',
    'session_id', 'chamber', 'n_actions', 'last_action_text',
]
MANIFEST_KEEP = [
    'bill_id', 'status', 'status_desc', 'committee',
    'committee_id', 'last_action', 'is_terminal',
]
PRED_KEEP = [
    'bill_id', 'passage_prob', 'n_actions', 'reached_vote', 'momentum', 'generated_at',
]
TRAIL_KEEP = [
    'bill_id', 'action_index', 'action_date', 'action_text', 'y_prob',
]


def build_snapshot():
    log.info('=' * 60)
    log.info('Building Active Bills App Snapshot')
    log.info('=' * 60)

    # ── Step 1: Gold features ─────────────────────────────────────────────────
    log.info('[1/6] Loading gold features (session %s)...', ACTIVE_SESSION)
    gold = pd.read_parquet(GOLD_URLS[ACTIVE_SESSION])
    gold['bill_id'] = gold['bill_id'].astype(str)
    gold = gold[[c for c in GOLD_KEEP if c in gold.columns]].copy()
    log.info('      %s bills, %s columns', f'{len(gold):,}', len(gold.columns))

    # ── Step 2: Manifest ──────────────────────────────────────────────────────
    log.info('[2/6] Loading manifest (session %s)...', ACTIVE_SESSION)
    manifest = pd.read_parquet(MANIFEST_URLS[ACTIVE_SESSION])
    manifest['bill_id'] = manifest['bill_id'].astype(str)
    manifest = manifest[[c for c in MANIFEST_KEEP if c in manifest.columns]].copy()
    log.info('      %s rows', f'{len(manifest):,}')

    # ── Step 3: Predictions ───────────────────────────────────────────────────
    log.info('[3/6] Loading predictions (session %s)...', ACTIVE_SESSION)
    pred = pd.read_parquet(PREDICTION_URLS[ACTIVE_SESSION])
    pred['bill_id'] = pred['bill_id'].astype(str)
    # Rename y_prob → passage_prob (matches predictions_loader convention)
    if 'y_prob' in pred.columns and 'passage_prob' not in pred.columns:
        pred = pred.rename(columns={'y_prob': 'passage_prob'})
    pred = pred[[c for c in PRED_KEEP if c in pred.columns]].copy()
    log.info('      %s bills', f'{len(pred):,}')

    # ── Step 4: Join everything ───────────────────────────────────────────────
    log.info('[4/6] Joining gold + manifest + predictions...')

    # Drop overlap columns (avoid duplicates) before merging manifest
    overlap = [c for c in manifest.columns if c != 'bill_id' and c in gold.columns]
    if overlap:
        gold = gold.drop(columns=overlap)

    snap = gold.merge(manifest, on='bill_id', how='left')
    snap = snap.merge(pred, on='bill_id', how='left', suffixes=('', '_pred'))

    # Enrollment flag — canonical signal from LegiScan status_desc
    snap['_enrolled'] = (
        snap['status_desc'].fillna('').astype(str).str.strip().str.lower() == 'enrolled'
    )
    # Pin enrolled bills (no Stage 2 score) to 1.0 so they appear in the card list
    enrolled_no_pred = snap['_enrolled'] & snap['passage_prob'].isna()
    if enrolled_no_pred.any():
        snap.loc[enrolled_no_pred, 'passage_prob'] = 1.0
        log.info('      Pinned %s enrolled bills to 1.0', enrolled_no_pred.sum())

    # Keep only bills that have a prediction (includes enrolled ones pinned above)
    snap = snap[snap['passage_prob'].notna()].copy()

    log.info('      Snapshot: %s active bills, %s columns',
             f'{len(snap):,}', len(snap.columns))

    # ── Step 5: Trail — filter to active session bills only ───────────────────
    log.info('[5/6] Loading + filtering trail (432K rows → current session only)...')
    trail = pd.read_parquet(TRAIL_URL)
    trail['bill_id'] = trail['bill_id'].astype(str)

    active_ids = set(snap['bill_id'])
    trail = trail[trail['bill_id'].isin(active_ids)].copy()
    trail = trail[[c for c in TRAIL_KEEP if c in trail.columns]].copy()

    log.info('      Trail filtered: %s rows (was 432K+)', f'{len(trail):,}')

    # ── Step 6: Upload ────────────────────────────────────────────────────────
    log.info('[6/6] Uploading to Azure gold blob...')
    b = BlobClient()

    b.write_parquet(snap, TIER_GOLD, SNAP_PATH)
    log.info('      Uploaded snapshot  → gold/%s', SNAP_PATH)

    b.write_parquet(trail, TIER_GOLD, TRAIL_PATH)
    log.info('      Uploaded trail     → gold/%s', TRAIL_PATH)

    log.info('=' * 60)
    log.info('DONE.')
    log.info('')
    log.info('Next step: generate SAS read URLs for the two new blobs,')
    log.info('then add them to data_catalog.py:')
    log.info(__import__("os").environ.get('LEG_COMPONENTS_PIPELINE_BUILD_APP_SNAPSHOT_PY_VALUE_L147', ""))
    log.info(__import__("os").environ.get('LEG_COMPONENTS_PIPELINE_BUILD_APP_SNAPSHOT_PY_VALUE_L148', ""))
    log.info('')
    log.info('Blob paths to generate URLs for:')
    log.info('    gold/%s', SNAP_PATH)
    log.info('    gold/%s', TRAIL_PATH)
    log.info('=' * 60)

    return snap, trail


if __name__ == '__main__':
    snap, trail = build_snapshot()
    print(f'\nSnapshot shape : {snap.shape}')
    print(f'Snapshot cols  : {list(snap.columns)}')
    print(f'Trail shape    : {trail.shape}')
    print(f'Trail cols     : {list(trail.columns)}')
