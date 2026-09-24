from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from rfnry_senses.models import Content, Identity


class _Event(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope: str
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Heard(_Event):
    kind: Literal["heard"] = "heard"
    text: str
    final: bool
    speaker: Identity


class TurnEnded(_Event):
    kind: Literal["turn_ended"] = "turn_ended"
    text: str
    speaker: Identity
    probability: float | None = None


class Said(_Event):
    kind: Literal["said"] = "said"
    text: str
    heard: str
    interrupted: bool
    triggered_by: Identity | None


class Interrupted(_Event):
    kind: Literal["interrupted"] = "interrupted"
    heard: str
    triggered_by: Identity


class Speaking(_Event):
    kind: Literal["speaking"] = "speaking"


class Silent(_Event):
    kind: Literal["silent"] = "silent"


class Looked(_Event):
    kind: Literal["looked"] = "looked"
    frames: int
    triggered_by: Identity


class Muted(_Event):
    kind: Literal["muted"] = "muted"
    muted: bool
    triggered_by: Identity


class Informed(_Event):
    kind: Literal["informed"] = "informed"
    text: str
    triggered_by: Identity


class ToolCalled(_Event):
    kind: Literal["tool_called"] = "tool_called"
    name: str
    input: dict[str, Any]
    output: Content
    is_error: bool


class Closed(_Event):
    kind: Literal["closed"] = "closed"
    reason: Literal["closed", "hung_up", "failed"]
    message: str | None = None


Event = Annotated[
    Heard
    | TurnEnded
    | Said
    | Interrupted
    | Speaking
    | Silent
    | Looked
    | Muted
    | Informed
    | ToolCalled
    | Closed,
    Field(discriminator="kind"),
]
