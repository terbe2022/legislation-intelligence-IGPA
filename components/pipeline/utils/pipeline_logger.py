# utils/pipeline_logger.py  — Legislation Pipeline
#
# Full implementation of the canonical PipelineLogger.
# Drop-in replacement for the stub in utils/pipeline_logger.py.
#
# Produces identical schema to the proposal_processing logger so the
# job_execution_monitor reads this pipeline's logs without any changes.
#
# Usage:
#
#   from utils.pipeline_logger import PipelineLogger
#
#   pl = PipelineLogger(
#       workflow_name="legislation_pipeline",
#       log_dir=Path(".../Legislation_Pipeline/logs"),
#       pipeline_version="2026-06-17",
#   )
#   with pl.pipeline_run():
#       with pl.stage("ingestion", order=1):
#           run_ingestion()
#           pl.set_stage_metadata(input_count=4300, output_count=4300)
#
#       with pl.stage("chunker", order=2):
#           run_chunker()
#           pl.set_stage_metadata(input_count=4300, output_count=51_000)
#
#       with pl.stage("extractor", order=3):
#           result = run_extractor(cfg, session_id)
#           pl.set_stage_metadata(
#               input_count=result["chunks_input"],
#               output_count=result["chunks_extracted"],
#               rows_written=result["chunks_extracted"],
#               extra={"chunks_failed": result["chunks_failed"]},
#           )

from __future__ import annotations

import getpass
import socket
import sys
import traceback as tb
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import pandas as pd

# ── Schema — must match REQUIRED_COLUMNS in monitor's log_reader.py ──────────

REQUIRED_COLUMNS = [
    "workflow_name", "pipeline_version", "run_id",
    "script_name", "script_order",
    "status", "is_success", "failure_stage",
    "event_time", "start_time", "end_time", "duration_seconds", "logged_at",
    "doc_count", "time_per_doc",
    "input_count", "output_count", "rows_written",
    "skip_reason",
    "input_file", "output_file", "input_file_exists", "output_file_written",
    "error_type", "error_message", "traceback",
    "hostname", "user", "python_version", "working_directory",
]

STATUS_SUCCESS         = "SUCCESS"
STATUS_FAILED          = "FAILED"
STATUS_UPSTREAM_FAILED = "UPSTREAM_FAILED"
STATUS_PIPELINE_START  = "PIPELINE_START"
STATUS_PIPELINE_END    = "PIPELINE_END"
SCRIPT_NAME_PIPELINE   = "PIPELINE"


def _capture_env() -> dict:
    return {
        "hostname":          socket.gethostname(),
        "user":              getpass.getuser(),
        "python_version":    sys.version.split()[0],
        "working_directory": str(Path.cwd()),
    }


class PipelineLogger:
    """Canonical logger for the Legislation Pipeline.

    Writes per-stage rows to logs/pipeline_log.parquet in the schema
    the job_execution_monitor expects. Never raises — a logging failure
    must never crash the pipeline.
    """

    def __init__(
        self,
        workflow_name: str,
        log_dir: Path,
        pipeline_version: str = "unknown",
    ) -> None:
        self.workflow_name    = workflow_name
        self.pipeline_version = pipeline_version
        self.run_id           = str(uuid.uuid4())
        self.log_path         = Path(log_dir) / "pipeline_log.parquet"
        self._env             = _capture_env()
        self._stage_meta: Dict[str, Any] = {}
        self._pipeline_failed = False

        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Public API ───────────────────────────────────────────────────────────

    def set_stage_metadata(
        self,
        input_count:         Optional[int]   = None,
        output_count:        Optional[int]   = None,
        rows_written:        Optional[int]   = None,
        doc_count:           Optional[int]   = None,
        time_per_doc:        Optional[float] = None,
        skip_reason:         Optional[str]   = None,
        input_file:          Optional[str]   = None,
        output_file:         Optional[str]   = None,
        input_file_exists:   Optional[bool]  = None,
        output_file_written: Optional[bool]  = None,
        extra:               Optional[dict]  = None,
    ) -> None:
        """Call inside a `with pl.stage(...)` block to attach metadata.
        Can be called multiple times; later calls overwrite earlier ones.
        `extra` accepts arbitrary keys stored as JSON in warning_flags.
        """
        self._stage_meta.update({
            k: v for k, v in {
                "input_count":         input_count,
                "output_count":        output_count,
                "rows_written":        rows_written,
                "doc_count":           doc_count,
                "time_per_doc":        time_per_doc,
                "skip_reason":         skip_reason,
                "input_file":          input_file,
                "output_file":         output_file,
                "input_file_exists":   input_file_exists,
                "output_file_written": output_file_written,
            }.items() if v is not None
        })
        if extra:
            import json
            self._stage_meta["warning_flags"] = json.dumps(extra)

    @contextmanager
    def pipeline_run(self) -> Iterator[None]:
        """Wrap the entire pipeline. Writes PIPELINE_START and PIPELINE_END
        sentinel rows so the monitor's silence-detection works.
        """
        start = datetime.now(timezone.utc)
        self._write_row(
            script_name=SCRIPT_NAME_PIPELINE,
            script_order=0,
            status=STATUS_PIPELINE_START,
            is_success=False,
            start_time=start,
            end_time=start,
            duration_seconds=0.0,
        )
        try:
            yield
        finally:
            end = datetime.now(timezone.utc)
            final_status = STATUS_FAILED if self._pipeline_failed else STATUS_PIPELINE_END
            self._write_row(
                script_name=SCRIPT_NAME_PIPELINE,
                script_order=9999,
                status=final_status,
                is_success=not self._pipeline_failed,
                start_time=start,
                end_time=end,
                duration_seconds=(end - start).total_seconds(),
            )

    @contextmanager
    def stage(self, stage_name: str, order: int = 0) -> Iterator[None]:
        """Context manager for one pipeline stage.

        On clean exit  → writes SUCCESS row.
        On exception   → writes FAILED row, marks pipeline as failed,
                         re-raises so the pipeline's own error handling runs.
        If a prior stage already failed → writes UPSTREAM_FAILED row.
        """
        self._stage_meta = {}
        start = datetime.now(timezone.utc)

        if self._pipeline_failed:
            end = datetime.now(timezone.utc)
            self._write_row(
                script_name=stage_name,
                script_order=order,
                status=STATUS_UPSTREAM_FAILED,
                is_success=False,
                start_time=start,
                end_time=end,
                duration_seconds=0.0,
            )
            return

        try:
            yield
            end = datetime.now(timezone.utc)
            self._write_row(
                script_name=stage_name,
                script_order=order,
                status=STATUS_SUCCESS,
                is_success=True,
                start_time=start,
                end_time=end,
                duration_seconds=(end - start).total_seconds(),
                **self._stage_meta,
            )
        except Exception as exc:
            end = datetime.now(timezone.utc)
            self._pipeline_failed = True
            self._write_row(
                script_name=stage_name,
                script_order=order,
                status=STATUS_FAILED,
                is_success=False,
                start_time=start,
                end_time=end,
                duration_seconds=(end - start).total_seconds(),
                error_type=type(exc).__name__,
                error_message=str(exc),
                traceback_str=tb.format_exc(),
                failure_stage=stage_name,
                **self._stage_meta,
            )
            raise

    # ── Internal write ───────────────────────────────────────────────────────

    def _write_row(
        self,
        script_name:         str,
        script_order:        int,
        status:              str,
        is_success:          bool,
        start_time:          datetime,
        end_time:            datetime,
        duration_seconds:    float,
        doc_count:           Optional[int]   = None,
        time_per_doc:        Optional[float] = None,
        input_count:         Optional[int]   = None,
        output_count:        Optional[int]   = None,
        rows_written:        Optional[int]   = None,
        skip_reason:         Optional[str]   = None,
        input_file:          Optional[str]   = None,
        output_file:         Optional[str]   = None,
        input_file_exists:   Optional[bool]  = None,
        output_file_written: Optional[bool]  = None,
        error_type:          Optional[str]   = None,
        error_message:       Optional[str]   = None,
        traceback_str:       Optional[str]   = None,
        failure_stage:       Optional[str]   = None,
        warning_flags:       Optional[str]   = None,
        **_extra,
    ) -> None:
        """Append one row. Defensive: never raises."""
        try:
            now = datetime.now(timezone.utc)
            row = {
                "workflow_name":     self.workflow_name,
                "pipeline_version":  self.pipeline_version,
                "run_id":            self.run_id,
                "script_name":       script_name,
                "script_order":      script_order,
                "status":            status,
                "is_success":        is_success,
                "failure_stage":     failure_stage,
                "event_time":        now,
                "start_time":        start_time,
                "end_time":          end_time,
                "duration_seconds":  duration_seconds,
                "logged_at":         now,
                "doc_count":         doc_count,
                "time_per_doc":      time_per_doc,
                "input_count":       input_count,
                "output_count":      output_count,
                "rows_written":      rows_written,
                "skip_reason":       skip_reason,
                "input_file":        input_file,
                "output_file":       output_file,
                "input_file_exists": input_file_exists,
                "output_file_written": output_file_written,
                "error_type":        error_type,
                "error_message":     error_message,
                "traceback":         traceback_str,
                "warning_flags":     warning_flags,
                **self._env,
            }

            new_df = pd.DataFrame([row])

            if self.log_path.exists():
                try:
                    existing = pd.read_parquet(self.log_path)
                    for col in REQUIRED_COLUMNS:
                        if col not in existing.columns:
                            existing[col] = None
                    combined = pd.concat([existing, new_df], ignore_index=True)
                except Exception:
                    combined = new_df
            else:
                combined = new_df

            combined.to_parquet(self.log_path, index=False)

        except Exception as logging_error:
            print(f"[LOGGER ERROR] Failed to write log row for '{script_name}': {logging_error}")
