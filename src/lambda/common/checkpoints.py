"""Durable stage checkpoints for the CaseAnalysis workflow.

Each completed inference stage stores its validated output at
`account_number=<id>/case_number=<displayId>/checkpoints/<stage>.json` in the raw bucket. A
checkpoint is reusable only when it is for the same case, model and prompt version and was made
from the same input (the rendered conversation for the summary, the summary for the
classifiers), and its output still passes validation. Anything else is treated as absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger

from common.analysis import STAGE_LIFECYCLE, STAGE_RCA, STAGE_SUMMARY
from common.case_refs import SummaryRef
from common.classification import REASON_MAX_CHARS
from common.processed import case_record_key
from common.s3_json import (
    S3JsonDecodeError,
    S3ObjectTooLargeError,
    S3PreconditionFailedError,
    read_json_if_exists,
    write_json,
)
from common.summary import check_summary
from common.taxonomy import LIFECYCLE, RCA, UNCLASSIFIED, Taxonomy

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

logger = Logger(child=True)

CHECKPOINT_SCHEMA_VERSION: Final = 1
CHECKPOINT_DIRECTORY: Final = "checkpoints"
MAX_CHECKPOINT_BYTES: Final = 64 * 1024
FALLBACK_FIELD: Final = "fallback"
STAGES: Final = (STAGE_SUMMARY, STAGE_RCA, STAGE_LIFECYCLE)
CLASSIFIER_TAXONOMIES: Final[dict[str, Taxonomy]] = {STAGE_RCA: RCA, STAGE_LIFECYCLE: LIFECYCLE}
_DOCUMENT_KEYS: Final = frozenset(
    {
        "schemaVersion",
        "stage",
        "accountId",
        "caseId",
        "displayId",
        "modelId",
        "promptVersion",
        "inputDigest",
        "completedAt",
        "output",
    }
)


def checkpoint_key(account_id: str, display_id: str, stage: str) -> str:
    """Return `account_number=<id>/case_number=<displayId>/checkpoints/<stage>.json`."""
    if stage not in STAGES:
        raise ValueError(f"Unknown stage {stage!r}")
    prefix = case_record_key(account_id, display_id).rsplit("/", 1)[0]
    return f"{prefix}/{CHECKPOINT_DIRECTORY}/{stage}.json"


def output_issue(stage: str, output: object) -> str | None:
    """Return why `output` is not a valid result for `stage`, or `None` when it is valid."""
    if not isinstance(output, dict):
        return "output_not_object"
    if stage == STAGE_SUMMARY:
        return _summary_issue(output)
    return _classification_issue(CLASSIFIER_TAXONOMIES[stage], output)


def _summary_issue(output: dict[str, Any]) -> str | None:
    summary = output.get("Case_Summary")
    if set(output) != {"Case_Summary"} or not isinstance(summary, str):
        return "summary_fields"
    check = check_summary(summary)
    return None if check.ok and check.text == summary else "summary_invalid"


def _classification_issue(taxonomy: Taxonomy, output: dict[str, Any]) -> str | None:
    if set(output) != {taxonomy.category_key, taxonomy.reason_key, FALLBACK_FIELD}:
        return "classification_fields"
    category = output[taxonomy.category_key]
    reason = output[taxonomy.reason_key]
    fallback = output[FALLBACK_FIELD]
    if not isinstance(category, str) or not taxonomy.is_record_value(category):
        return "category_invalid"
    if not isinstance(fallback, bool) or fallback != (category == UNCLASSIFIED):
        return "fallback_mismatch"
    if not isinstance(reason, str) or not reason.strip() or len(reason) > REASON_MAX_CHARS:
        return "reason_invalid"
    return None


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """A validated stage result plus the identity of the inputs it was produced from."""

    stage: str
    account_id: str
    case_id: str
    display_id: str
    model_id: str
    prompt_version: str
    input_digest: str
    completed_at: str
    output: dict[str, Any]

    @property
    def key(self) -> str:
        return checkpoint_key(self.account_id, self.display_id, self.stage)

    def to_document(self) -> dict[str, Any]:
        return {
            "schemaVersion": CHECKPOINT_SCHEMA_VERSION,
            "stage": self.stage,
            "accountId": self.account_id,
            "caseId": self.case_id,
            "displayId": self.display_id,
            "modelId": self.model_id,
            "promptVersion": self.prompt_version,
            "inputDigest": self.input_digest,
            "completedAt": self.completed_at,
            "output": self.output,
        }

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> Checkpoint | None:
        """Parse a stored document; `None` when its shape is wrong."""
        if set(document) != _DOCUMENT_KEYS or document["schemaVersion"] != (
            CHECKPOINT_SCHEMA_VERSION
        ):
            return None
        strings = [k for k in _DOCUMENT_KEYS if k not in {"schemaVersion", "output"}]
        if not all(isinstance(document[k], str) and document[k] for k in strings):
            return None
        if not isinstance(document["output"], dict):
            return None
        return cls(
            stage=document["stage"],
            account_id=document["accountId"],
            case_id=document["caseId"],
            display_id=document["displayId"],
            model_id=document["modelId"],
            prompt_version=document["promptVersion"],
            input_digest=document["inputDigest"],
            completed_at=document["completedAt"],
            output=document["output"],
        )


@dataclass(frozen=True, slots=True)
class Expectation:
    """What a reusable checkpoint must match. `input_digest=None` skips the input check."""

    stage: str
    ref: SummaryRef
    model_id: str
    prompt_version: str
    input_digest: str | None

    def mismatch(self, checkpoint: Checkpoint) -> str | None:
        """Return why `checkpoint` cannot be reused, or `None` when it can."""
        if (checkpoint.stage, checkpoint.account_id, checkpoint.case_id, checkpoint.display_id) != (
            self.stage,
            self.ref.account_id,
            self.ref.case_id,
            self.ref.display_id,
        ):
            return "case_mismatch"
        if checkpoint.model_id != self.model_id:
            return "model_changed"
        if checkpoint.prompt_version != self.prompt_version:
            return "prompt_version_changed"
        if self.input_digest is not None and checkpoint.input_digest != self.input_digest:
            return "input_changed"
        return output_issue(self.stage, checkpoint.output)


def load_checkpoint(s3: S3Client, bucket: str, expected: Expectation) -> Checkpoint | None:
    """Return the stage checkpoint if it exists and is reusable for `expected`, else `None`."""
    key = checkpoint_key(expected.ref.account_id, expected.ref.display_id, expected.stage)
    try:
        document = read_json_if_exists(s3, bucket, key, max_bytes=MAX_CHECKPOINT_BYTES)
    except (S3JsonDecodeError, S3ObjectTooLargeError):
        reason: str | None = "unreadable"
    else:
        if document is None:
            return None
        checkpoint = Checkpoint.from_document(document)
        reason = "malformed" if checkpoint is None else expected.mismatch(checkpoint)
        if checkpoint is not None and reason is None:
            return checkpoint
    logger.info(
        "Stage checkpoint is not reusable", extra={"stage": expected.stage, "reason": reason}
    )
    return None


def save_checkpoint(
    s3: S3Client, bucket: str, checkpoint: Checkpoint, ref: SummaryRef
) -> Checkpoint:
    """Store `checkpoint` and return the checkpoint later stages should use.

    The first write is conditional, so when a concurrent attempt for the same case has already
    stored a reusable checkpoint, that one wins and is returned. A stale checkpoint (another model,
    prompt version or input) is replaced.
    """
    issue = output_issue(checkpoint.stage, checkpoint.output)
    if issue is not None:
        raise ValueError(f"Refusing to store an invalid {checkpoint.stage} checkpoint ({issue})")
    document = checkpoint.to_document()
    try:
        write_json(
            s3, bucket, checkpoint.key, document, if_none_match=True, max_bytes=MAX_CHECKPOINT_BYTES
        )
    except S3PreconditionFailedError:
        existing = load_checkpoint(
            s3,
            bucket,
            Expectation(
                stage=checkpoint.stage,
                ref=ref,
                model_id=checkpoint.model_id,
                prompt_version=checkpoint.prompt_version,
                input_digest=checkpoint.input_digest,
            ),
        )
        if existing is not None:
            return existing
        write_json(s3, bucket, checkpoint.key, document, max_bytes=MAX_CHECKPOINT_BYTES)
    return checkpoint
