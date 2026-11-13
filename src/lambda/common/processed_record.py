"""The enriched processed record: exactly 18 fields, validated before it is committed.

A processed record merges the case's original metadata from the raw bucket with the summary,
RCA and lifecycle results and their provenance. It is written once, conditionally, to
`account_number=<id>/case_number=<displayId>/data.json` in the processed bucket.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

from common.bedrock import SUPPORTED_MODEL_IDS
from common.checkpoints import Checkpoint
from common.classification import REASON_MAX_CHARS
from common.summary import check_summary
from common.taxonomy import LIFECYCLE, RCA, Taxonomy

ORIGINAL_METADATA_FIELDS: Final = (
    "caseId",
    "displayId",
    "subject",
    "serviceCode",
    "categoryCode",
    "severityCode",
    "submittedBy",
    "timeCreated",
)
PROCESSED_FIELDS: Final = (
    *ORIGINAL_METADATA_FIELDS,
    "Case_Retrieval_Date",
    "Case_Summary",
    "RCA_Category",
    "RCA_Reason",
    "RCA_Retrieval_Date",
    "Lifecycle_Category",
    "Lifecycle_Reason",
    "Lifecycle_Retrieval_Date",
    "Model_Id",
    "Prompt_Version",
)
# Descriptive metadata is preserved exactly as DescribeCases returned it, including null.
NULLABLE_FIELDS: Final = frozenset(
    {"subject", "serviceCode", "categoryCode", "severityCode", "submittedBy"}
)
DATE_FIELDS: Final = ("Case_Retrieval_Date", "RCA_Retrieval_Date", "Lifecycle_Retrieval_Date")
_UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_PROMPT_VERSION = re.compile(r"^pv-[0-9a-f]{16}$")


class IncompleteRecordError(ValueError):
    """The record is incomplete or inconsistent. `code` and `field` never contain values."""

    def __init__(self, code: str, field: str | None = None) -> None:
        super().__init__(f"Processed record invalid: {code}{f' ({field})' if field else ''}")
        self.code = code
        self.field = field


def _classification(record: Mapping[str, Any], taxonomy: Taxonomy) -> None:
    category = record[taxonomy.category_key]
    if not taxonomy.is_record_value(category):
        raise IncompleteRecordError("invalid_category", taxonomy.category_key)
    reason = record[taxonomy.reason_key]
    if len(reason) > REASON_MAX_CHARS:
        raise IncompleteRecordError("reason_too_long", taxonomy.reason_key)


def validate_processed_record(record: Mapping[str, Any]) -> None:
    """Raise `IncompleteRecordError` unless `record` is a complete, consistent processed record."""
    if set(record) != set(PROCESSED_FIELDS):
        raise IncompleteRecordError("field_set")
    for field in PROCESSED_FIELDS:
        value = record[field]
        if value is None and field in NULLABLE_FIELDS:
            continue
        if not isinstance(value, str):
            raise IncompleteRecordError("not_a_string", field)
        if field not in NULLABLE_FIELDS and not value.strip():
            raise IncompleteRecordError("empty", field)
    for field in DATE_FIELDS:
        if not _UTC_TIMESTAMP.match(record[field]):
            raise IncompleteRecordError("invalid_date", field)
    if not check_summary(record["Case_Summary"]).ok:
        raise IncompleteRecordError("invalid_summary", "Case_Summary")
    _classification(record, RCA)
    _classification(record, LIFECYCLE)
    if record["Model_Id"] not in SUPPORTED_MODEL_IDS:
        raise IncompleteRecordError("unsupported_model", "Model_Id")
    if not _PROMPT_VERSION.match(record["Prompt_Version"]):
        raise IncompleteRecordError("invalid_prompt_version", "Prompt_Version")


def build_processed_record(
    raw_metadata: Mapping[str, Any],
    summary: Checkpoint,
    rca: Checkpoint,
    lifecycle: Checkpoint,
) -> dict[str, Any]:
    """Merge original metadata and stage results into a validated 18-field record.

    All three checkpoints must share one model and prompt version, which become `Model_Id` and
    `Prompt_Version`; the classification dates are the times each classification completed.
    """
    provenance = {(c.model_id, c.prompt_version) for c in (summary, rca, lifecycle)}
    if len(provenance) != 1:
        raise IncompleteRecordError("mixed_provenance")
    missing = [field for field in ORIGINAL_METADATA_FIELDS if field not in raw_metadata]
    if missing or "Case_Retrieval_Date" not in raw_metadata:
        raise IncompleteRecordError("raw_metadata_fields")
    ((model_id, version),) = provenance
    record: dict[str, Any] = {field: raw_metadata[field] for field in ORIGINAL_METADATA_FIELDS}
    record |= {
        "Case_Retrieval_Date": raw_metadata["Case_Retrieval_Date"],
        "Case_Summary": summary.output["Case_Summary"],
        "RCA_Category": rca.output[RCA.category_key],
        "RCA_Reason": rca.output[RCA.reason_key],
        "RCA_Retrieval_Date": rca.completed_at,
        "Lifecycle_Category": lifecycle.output[LIFECYCLE.category_key],
        "Lifecycle_Reason": lifecycle.output[LIFECYCLE.reason_key],
        "Lifecycle_Retrieval_Date": lifecycle.completed_at,
        "Model_Id": model_id,
        "Prompt_Version": version,
    }
    validate_processed_record(record)
    return record
