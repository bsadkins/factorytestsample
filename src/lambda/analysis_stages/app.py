"""Analysis Stages: the single Lambda function behind the three Bedrock workflow tasks.

Summary, RCA and Lifecycle keep their focused handler modules (`case_summary`, `case_rca`,
`case_lifecycle`); this entry point selects one from the task payload's `stage` field. Hosting
them in one function lets its reserved concurrency bound the aggregate number of concurrent
Bedrock invocations to exactly `bedrockMaxConcurrency`: each invocation runs one stage, and a
stage makes its Bedrock calls one at a time. Separate functions would each need their own
reservation, multiplying the configured cap or making small caps impossible to honour.

Invocations over the cap are throttled by Lambda and retried by the workflow with bounded
backoff, so the cap is a limit on in-flight inference rather than on running workflows.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final

from case_lifecycle import app as lifecycle_app
from case_rca import app as rca_app
from case_summary import app as summary_app
from common.analysis import (
    STAGE_LIFECYCLE,
    STAGE_RCA,
    STAGE_SUMMARY,
    InvalidWorkflowInputError,
)

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

STAGE_FIELD: Final = "stage"
STAGE_HANDLERS: Final[Mapping[str, Callable[[dict[str, Any], Any], dict[str, Any]]]] = {
    STAGE_SUMMARY: summary_app.handler,
    STAGE_RCA: rca_app.handler,
    STAGE_LIFECYCLE: lifecycle_app.handler,
}


def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Run the stage named in `event["stage"]`; an unknown stage fails like invalid input."""
    stage = event.get(STAGE_FIELD) if isinstance(event, Mapping) else None
    stage_handler = STAGE_HANDLERS.get(stage) if isinstance(stage, str) else None
    if stage_handler is None:
        raise InvalidWorkflowInputError("unknown_stage")
    return stage_handler(event, context)
