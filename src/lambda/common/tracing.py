"""Keep exception messages out of AWS X-Ray traces.

The X-Ray SDK records `str(exception)` for every exception added to a trace entity, including
the botocore subsegments it creates automatically when Powertools Tracer patches the SDK. That
happens inside the AWS call, before application code can sanitise the error, and AWS service
errors can echo request content such as SQL, prompts or case text. Traces therefore keep each
exception's type, stack frames and the entity's fault/error flags, but never its message.
"""

from __future__ import annotations

import functools
from typing import Any, Final

from aws_xray_sdk.core.models.entity import Entity  # type: ignore[import-untyped]

_INSTALLED: Final = "_exception_messages_withheld"


def withhold_exception_messages() -> None:
    """Make every X-Ray entity in this process drop exception messages when it records them.

    The SDK has no setting for this, so `Entity.add_exception` (used by segments, subsegments
    and the botocore patch alike) is wrapped once per process; repeated calls are no-ops.
    """
    original = Entity.add_exception
    if getattr(original, _INSTALLED, False):
        return

    @functools.wraps(original)
    def add_exception(
        self: Any,  # noqa: ANN401
        exception: BaseException,
        stack: object,
        remote: bool = False,
    ) -> None:
        original(self, exception, stack, remote)
        cause = getattr(self, "cause", None)
        if isinstance(cause, dict):
            for throwable in cause.get("exceptions", ()):
                vars(throwable).pop("message", None)

    setattr(add_exception, _INSTALLED, True)
    Entity.add_exception = add_exception
