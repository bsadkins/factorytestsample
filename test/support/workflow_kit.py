"""Step Functions stand-in for workflow starter and cleanup tests.

`FakeStepFunctions` follows the documented Standard-workflow semantics the handlers depend on:
StartExecution with the name and input of a RUNNING execution succeeds idempotently, while a
closed execution or different input yields `ExecutionAlreadyExists`; unknown executions yield
`ExecutionDoesNotExist`; ListExecutions pages with `nextToken`. Calls can be failed or given a
side effect to simulate races and service errors.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from botocore.exceptions import ClientError

REGION = "us-east-1"
ACCOUNT = "123456789012"
STATE_MACHINE_ARN = f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:CaseAnalysis-test"


def sfn_error(code: str, operation: str, status: int = 400) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": "synthetic"},
            "ResponseMetadata": {"HTTPStatusCode": status},  # type: ignore[typeddict-item]
        },
        operation,
    )


@dataclass
class Execution:
    name: str
    input: str
    status: str = "RUNNING"

    @property
    def arn(self) -> str:
        return f"arn:aws:states:{REGION}:{ACCOUNT}:execution:CaseAnalysis-test:{self.name}"


class _Paginator:
    def __init__(self, method: Callable[..., dict[str, Any]], page_size: int) -> None:
        self._method = method
        self._page_size = page_size

    def paginate(self, **kwargs: Any) -> Iterator[dict[str, Any]]:  # noqa: ANN401
        token: str | None = None
        while True:
            request = {**kwargs, "maxResults": self._page_size}
            if token is not None:
                request["nextToken"] = token
            page = self._method(**request)
            yield page
            token = page.get("nextToken")
            if token is None:
                return


@dataclass
class FakeStepFunctions:
    page_size: int = 2
    executions: dict[str, Execution] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    failures: dict[str, ClientError] = field(default_factory=dict)
    effects: dict[str, Callable[[], None]] = field(default_factory=dict)

    def add(self, name: str, status: str, input_document: str = "{}") -> Execution:
        execution = Execution(name=name, input=input_document, status=status)
        self.executions[name] = execution
        return execution

    def status(self, name: str) -> str | None:
        execution = self.executions.get(name)
        return execution.status if execution else None

    def started(self) -> list[str]:
        return [name for operation, name in self.calls if operation == "start"]

    def _before(self, operation: str, name: str) -> None:
        self.calls.append((operation, name))
        effect = self.effects.pop(operation, None)
        if effect is not None:
            effect()
        failure = self.failures.get(operation)
        if failure is not None:
            raise failure

    def start_execution(self, *, stateMachineArn: str, name: str, input: str) -> dict[str, Any]:  # noqa: N803
        assert stateMachineArn == STATE_MACHINE_ARN
        self._before("start", name)
        existing = self.executions.get(name)
        if existing is not None:
            if existing.status == "RUNNING" and existing.input == input:
                return {"executionArn": existing.arn}
            raise sfn_error("ExecutionAlreadyExists", "StartExecution")
        return {"executionArn": self.add(name, "RUNNING", input).arn}

    def describe_execution(self, *, executionArn: str) -> dict[str, Any]:  # noqa: N803
        prefix = STATE_MACHINE_ARN.replace(":stateMachine:", ":execution:") + ":"
        assert executionArn.startswith(prefix)
        name = executionArn.removeprefix(prefix)
        self._before("describe", name)
        execution = self.executions.get(name)
        if execution is None:
            raise sfn_error("ExecutionDoesNotExist", "DescribeExecution")
        return {"executionArn": execution.arn, "name": name, "status": execution.status}

    def list_executions(
        self,
        *,
        stateMachineArn: str,  # noqa: N803
        statusFilter: str,  # noqa: N803
        maxResults: int,  # noqa: N803
        nextToken: str | None = None,  # noqa: N803
    ) -> dict[str, Any]:
        assert stateMachineArn == STATE_MACHINE_ARN
        self._before("list", statusFilter)
        matching = [e for e in self.executions.values() if e.status == statusFilter]
        start = int(nextToken or 0)
        page = matching[start : start + maxResults]
        response: dict[str, Any] = {
            "executions": [{"name": e.name, "status": e.status} for e in page]
        }
        if start + maxResults < len(matching):
            response["nextToken"] = str(start + maxResults)
        return response

    def get_paginator(self, operation: str) -> _Paginator:
        assert operation == "list_executions"
        return _Paginator(self.list_executions, self.page_size)
