from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from rfnry_senses.models import Identity, Part
from rfnry_senses.session import Session
from rfnry_senses.tools import Tool, Tools


class LookArgs(BaseModel):
    frames: int = Field(default=1, ge=1, le=10, description="How many of the newest frames to see")


class TextArgs(BaseModel):
    text: str = Field(description="The words")


class NoArgs(BaseModel):
    pass


class SensesTools(Tools):
    def __init__(self, session: Session, *, triggered_by: Identity) -> None:
        self.session = session
        self.triggered_by = triggered_by
        tools: list[Tool] = []
        if session._window is not None:
            tools.append(
                Tool(
                    "look",
                    "See the camera: the newest frames, oldest first, each followed by when it was taken.",
                    body=self._look,
                    args=LookArgs,
                )
            )
        if session._voice:
            tools.append(
                Tool(
                    "say",
                    "Speak these exact words to the person. Returns what they heard before any interruption.",
                    body=self._say,
                    args=TextArgs,
                )
            )
        if session._live is not None:
            tools.append(
                Tool(
                    "prompt",
                    "Have the live voice say this, in its own words, now. Returns what it said and what was heard.",
                    body=self._prompt,
                    args=TextArgs,
                )
            )
            tools.append(
                Tool(
                    "inform",
                    "Tell the live voice something it should know, without it speaking.",
                    body=self._inform,
                    args=TextArgs,
                )
            )
        if session._voice:
            tools.append(
                Tool(
                    "interrupt",
                    "Stop speaking now. Returns what the person heard.",
                    body=self._interrupt,
                    args=NoArgs,
                )
            )
        super().__init__(tools)

    async def _look(self, args: dict[str, Any]) -> list[Part]:
        return await self.session.look(args.get("frames", 1), triggered_by=self.triggered_by)

    async def _say(self, args: dict[str, Any]) -> dict[str, Any]:
        said = await self.session.say(args["text"], triggered_by=self.triggered_by)
        return {"heard": said.heard, "interrupted": said.interrupted}

    async def _prompt(self, args: dict[str, Any]) -> dict[str, Any]:
        said = await self.session.prompt(args["text"], triggered_by=self.triggered_by)
        return {"said": said.text, "heard": said.heard, "interrupted": said.interrupted}

    async def _inform(self, args: dict[str, Any]) -> str:
        await self.session.inform(args["text"], triggered_by=self.triggered_by)
        return "informed"

    async def _interrupt(self, args: dict[str, Any]) -> dict[str, Any]:
        cut = await self.session.interrupt(triggered_by=self.triggered_by)
        return {"heard": cut.heard if cut is not None else ""}
