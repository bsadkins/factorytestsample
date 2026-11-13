"""Case Lifecycle: the operational lifecycle stage of the CaseAnalysis workflow.

Classifies the validated case summary into exactly one of the shared operational lifecycle
categories, with one corrective follow-up and then the explicit `Unclassified` fallback. See
`common.classifier_stage` for the shared stage logic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from common.analysis import STAGE_LIFECYCLE, AnalysisClients, AnalysisSettings
from common.aws import client_config
from common.bedrock import bedrock_runtime_client
from common.classifier_stage import run_classification_stage
from common.taxonomy import LIFECYCLE

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger()
tracer = Tracer()
metrics = Metrics()


@cache
def clients() -> AnalysisClients:
    return AnalysisClients(
        s3=boto3.client("s3", config=client_config()), bedrock=bedrock_runtime_client()
    )


def now() -> datetime:
    return datetime.now(UTC)


@logger.inject_lambda_context(log_event=False, clear_state=True)
@tracer.capture_lambda_handler(capture_response=False)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Lifecycle stage task: returns `{stage, status, accountId, displayId, checkpointKey}`."""
    return run_classification_stage(
        event,
        context,
        stage=STAGE_LIFECYCLE,
        taxonomy=LIFECYCLE,
        settings=AnalysisSettings.from_env(),
        aws=clients(),
        metrics=metrics,
        now=now,
    )
