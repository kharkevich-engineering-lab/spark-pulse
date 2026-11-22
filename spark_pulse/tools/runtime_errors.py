"""The errors a run raises when it cannot be planned or started.

Its own module so the helpers split out of :mod:`native_runtime` (placement
needs it for an exhausted port range) can raise the very class callers catch,
without importing the runtime they were split from.
"""

from __future__ import annotations


class NativeRuntimeError(RuntimeError):
    """A native deployment could not be planned or started."""


class MissingModelError(NativeRuntimeError):
    """The model a deployment needs is not in the local catalogue.

    A subclass, and not just a message, because this is the one planning
    failure with an obvious next step: fetch the model. Callers that can offer
    that need the model id as a value — parsing it back out of the sentence
    would be a contract nobody wrote down and the first reworded message would
    break it.
    """

    def __init__(self, model: str, message: str = "") -> None:
        self.model = model
        super().__init__(
            message
            or (
                f"model '{model}' is not in the local catalogue; "
                "download it first or deploy with allow_missing_model"
            )
        )
