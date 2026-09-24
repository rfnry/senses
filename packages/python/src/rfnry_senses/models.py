from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Annotated, Any, Literal, Protocol, Self, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_serializer,
    model_validator,
)

from rfnry_senses.errors import InputError, ModelError

State = str | dict[str, Any] | list[Any]


class Text(BaseModel):
    type: Literal["text"] = "text"
    text: str


class _Media(BaseModel):
    model_config = ConfigDict(val_json_bytes="base64")

    type: str
    media_type: str
    data: bytes | None = None
    url: str | None = None

    @model_validator(mode="after")
    def _data_or_url(self) -> Self:
        if (self.data is None) == (self.url is None):
            raise InputError(f"models: {self.type} carries data or url, not both")
        return self

    @field_serializer("data", when_used="json-unless-none")
    def _base64(self, data: bytes) -> str:
        return base64.b64encode(data).decode()


class Image(_Media):
    type: Literal["image"] = "image"


class Audio(_Media):
    type: Literal["audio"] = "audio"


class Document(_Media):
    type: Literal["document"] = "document"
    name: str | None = None


Part = Annotated[Text | Image | Audio | Document, Field(discriminator="type")]
Content = str | list[Part]


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]


class Usage(BaseModel):
    input: int = Field(default=0, ge=0)
    output: int = Field(default=0, ge=0)
    cache_creation: int = Field(default=0, ge=0)
    cache_read: int = Field(default=0, ge=0)

    model_config = {"extra": "forbid", "frozen": True}

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_creation=self.cache_creation + other.cache_creation,
            cache_read=self.cache_read + other.cache_read,
        )


class Choice(BaseModel):
    type: Literal["choice"] = "choice"
    instructions: str
    options: dict[str, str]

    @model_validator(mode="after")
    def _at_least_two(self) -> Choice:
        if len(self.options) < 2:
            raise ValueError("models: a choice needs at least two options")
        return self


class Score(BaseModel):
    type: Literal["score"] = "score"
    instructions: str
    levels: list[str]

    @model_validator(mode="after")
    def _at_least_two(self) -> Score:
        if len(self.levels) < 2:
            raise ValueError("models: a score needs at least two levels")
        return self


class Check(BaseModel):
    type: Literal["check"] = "check"
    instructions: str
    yes: str | None = None
    no: str | None = None


Question = Choice | Score | Check


class ChoiceAnswer(BaseModel):
    type: Literal["choice"] = "choice"
    choice: str
    probabilities: dict[str, float]
    confidence: float = 0.0


class ScoreAnswer(BaseModel):
    type: Literal["score"] = "score"
    score: float
    probabilities: list[float]
    confidence: float = 0.0


class CheckAnswer(BaseModel):
    type: Literal["check"] = "check"
    probability: float = Field(ge=0.0, le=1.0)
    confidence: float = 0.0


Answer = ChoiceAnswer | ScoreAnswer | CheckAnswer


class Decided(BaseModel):
    answers: dict[str, Answer]
    usage: Usage | None = None
    model: str | None = None


def as_decided(value: object) -> Decided:
    try:
        if isinstance(value, Mapping):
            return Decided.model_validate({"answers": value}, from_attributes=True)
        return Decided.model_validate(value, from_attributes=True)
    except ValidationError as exc:
        raise ModelError(f"models: the decision model's answers do not fit the contract: {exc.errors(include_url=False)}") from exc


class UsageLike(Protocol):
    @property
    def input(self) -> int: ...
    @property
    def output(self) -> int: ...
    @property
    def cache_creation(self) -> int: ...
    @property
    def cache_read(self) -> int: ...


class ChoiceAnswerLike(Protocol):
    @property
    def type(self) -> Literal["choice"]: ...
    @property
    def choice(self) -> str: ...
    @property
    def probabilities(self) -> dict[str, float]: ...


class ScoreAnswerLike(Protocol):
    @property
    def type(self) -> Literal["score"]: ...
    @property
    def score(self) -> float: ...
    @property
    def probabilities(self) -> list[float]: ...


class CheckAnswerLike(Protocol):
    @property
    def type(self) -> Literal["check"]: ...
    @property
    def probability(self) -> float: ...


AnswerLike = ChoiceAnswerLike | ScoreAnswerLike | CheckAnswerLike


class DecidedLike(Protocol):
    @property
    def answers(self) -> Mapping[str, AnswerLike]: ...
    @property
    def usage(self) -> UsageLike | None: ...
    @property
    def model(self) -> str | None: ...


@runtime_checkable
class DecisionModel(Protocol):
    async def decide(
        self, state: State, questions: dict[str, Question]
    ) -> Mapping[str, AnswerLike] | DecidedLike: ...


class Identity(BaseModel):
    role: Literal["user", "assistant", "system"]
    id: str
    name: str | None = None
    metadata: dict[str, Any] = {}


class IdentityLike(Protocol):
    @property
    def role(self) -> Literal["user", "assistant", "system"]: ...
    @property
    def id(self) -> str: ...
    @property
    def name(self) -> str | None: ...
    @property
    def metadata(self) -> dict[str, Any]: ...


def as_identity(value: IdentityLike) -> Identity:
    try:
        return Identity.model_validate(value, from_attributes=True)
    except ValidationError as exc:
        raise InputError(f"models: not an identity: {exc.errors(include_url=False)}") from exc
