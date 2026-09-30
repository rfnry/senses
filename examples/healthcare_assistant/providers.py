import asyncio
import contextlib
import json
import math
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from string import ascii_uppercase
from typing import Any

import httpx
from google import genai
from google.genai import types
from rfnry_engine import (
    Answer,
    CheckAnswer,
    ChoiceAnswer,
    Content,
    Decided,
    Image,
    QuestionLike,
    ScoreAnswer,
    State,
    ToolSpec,
    Usage,
)
from rfnry_senses import (
    AudioFrame,
    LiveAudio,
    LiveDone,
    LiveEvent,
    LiveFailed,
    LiveHeard,
    LiveInterrupted,
    LiveSaid,
    LiveStarted,
    LiveToolCall,
)


class TypeSafeProvider:
    url = "https://api.typesafe.ai/v1/systemone"
    retried = (429, 500, 502, 503, 529)
    most_options = 255
    most_levels = 10

    def __init__(self, model: str = "jev-latest", attempts: int = 4) -> None:
        self.model = model
        self.attempts = attempts
        self.client = httpx.AsyncClient(
            timeout=60, headers={"Authorization": f"Bearer {os.environ['DECISION_API_KEY']}"}
        )

    async def decide(self, state: State, questions: Mapping[str, QuestionLike]) -> Decided:
        body = {
            "state": state,
            "model": self.model,
            "questions": {qid: self._question(q) for qid, q in questions.items()},
        }
        for attempt in range(self.attempts):
            response = await self.client.post(self.url, json=body)
            if response.status_code not in self.retried or attempt == self.attempts - 1:
                break
            await asyncio.sleep(float(response.headers.get("retry-after", 2**attempt)))
        response.raise_for_status()
        data = response.json()
        usage = data.get("usage") or {}
        return Decided(
            answers={qid: self._answer(q, data["answers"][qid]) for qid, q in questions.items()},
            usage=Usage(input=usage.get("input_tokens", 0), output=usage.get("output_tokens", 0)),
            model=data.get("model"),
        )

    def _question(self, question: QuestionLike) -> dict[str, Any]:
        if question.type == "choice":
            if len(question.options) > self.most_options:
                raise ValueError(f"typesafe: a choice takes {self.most_options} options at most")
            return {
                "type": "choice",
                "instructions": question.instructions,
                "criteria": dict(question.options),
            }
        if question.type == "score":
            if len(question.levels) > self.most_levels:
                raise ValueError(f"typesafe: a score takes {self.most_levels} levels at most")
            return {
                "type": "score",
                "instructions": question.instructions,
                "criteria": list(question.levels),
            }
        body: dict[str, Any] = {"type": "noul", "instructions": question.instructions}
        if question.yes is not None or question.no is not None:
            body["criteria"] = {"true": question.yes, "false": question.no}
        return body

    def _answer(self, question: QuestionLike, raw: dict[str, Any]) -> Answer:
        if question.type == "choice":
            probabilities = self._normalized(
                {option: raw["probabilities"].get(option, 0.0) for option in question.options}
            )
            choice = raw.get("choice")
            if choice not in probabilities:
                choice = max(probabilities, key=lambda option: probabilities[option])
            return ChoiceAnswer(choice=choice, probabilities=probabilities)
        if question.type == "score":
            levels = self._normalized(
                {str(i): raw["probabilities"].get(str(i), 0.0) for i in range(len(question.levels))}
            )
            return ScoreAnswer(score=float(raw["score"]), probabilities=list(levels.values()))
        return CheckAnswer(probability=min(1.0, max(0.0, float(raw["noul"]))))

    def _normalized(self, probabilities: dict[str, float]) -> dict[str, float]:
        total = sum(float(value) for value in probabilities.values())
        if total <= 0:
            return {key: 1 / len(probabilities) for key in probabilities}
        return {key: float(value) / total for key, value in probabilities.items()}


class LocalDecisionProvider:
    retried = (429, 500, 502, 503)
    system = (
        "You read a state and answer one question about it by choosing one of the lettered "
        "options. Reply with the letter alone."
    )

    def __init__(
        self,
        model: str = "llama3.1:8b",
        url: str = "http://localhost:11434/v1",
        body: dict[str, Any] | None = None,
        attempts: int = 4,
    ) -> None:
        self.model = model
        self.url = f"{url.rstrip('/')}/chat/completions"
        self.body = body or {}
        self.attempts = attempts
        self.client = httpx.AsyncClient(timeout=120)

    async def decide(self, state: State, questions: Mapping[str, QuestionLike]) -> Decided:
        asked = await asyncio.gather(*(self._ask(state, q) for q in questions.values()))
        return Decided(
            answers={qid: answer for qid, (answer, _, _) in zip(questions, asked, strict=True)},
            usage=sum((usage for _, usage, _ in asked), Usage()),
            model=asked[0][2] if asked else self.model,
        )

    async def _ask(self, state: State, question: QuestionLike) -> tuple[Answer, Usage, str]:
        options = self._options(question)
        if len(options) > len(ascii_uppercase):
            raise ValueError(f"local: {len(options)} options, more than the 26 letters")
        letters = ascii_uppercase[: len(options)]
        listed = "\n".join(
            f"{letter}) {option}" for letter, option in zip(letters, options, strict=True)
        )
        allowed = f"{', '.join(letters[:-1])} or {letters[-1]}"
        text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
        prompt = (
            f"{text}\n\nQuestion: {self._shown(question.instructions)}\n\n{listed}\n\n"
            f"Answer with one letter: {allowed}."
        )
        data = await self._complete(prompt)
        probabilities = self._probabilities(data, letters)
        usage = data.get("usage") or {}
        spent = Usage(input=usage.get("prompt_tokens", 0), output=usage.get("completion_tokens", 0))
        return self._answer(question, probabilities), spent, data.get("model") or self.model

    def _options(self, question: QuestionLike) -> list[str]:
        if question.type == "choice":
            return [
                key if meaning is None else f"{key}: {self._shown(meaning)}"
                for key, meaning in question.options.items()
            ]
        if question.type == "score":
            return [self._shown(level) for level in question.levels]
        return [
            f"yes: {self._shown(question.yes)}" if question.yes else "yes",
            f"no: {self._shown(question.no)}" if question.no else "no",
        ]

    def _shown(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    async def _complete(self, prompt: str) -> dict[str, Any]:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 1,
            "temperature": 0,
            "logprobs": True,
            "top_logprobs": 20,
        } | self.body
        for attempt in range(self.attempts):
            response = await self.client.post(self.url, json=body)
            if response.status_code not in self.retried or attempt == self.attempts - 1:
                break
            await asyncio.sleep(float(response.headers.get("retry-after", 2**attempt)))
        response.raise_for_status()
        return response.json()

    def _probabilities(self, data: dict[str, Any], letters: str) -> list[float]:
        content = (data["choices"][0].get("logprobs") or {}).get("content") or []
        if not content or not content[0].get("top_logprobs"):
            raise ValueError("local: the server returned no top_logprobs for the answer token")
        candidates = content[0]["top_logprobs"]
        mass = dict.fromkeys(letters, 0.0)
        for candidate in candidates:
            token = candidate["token"].strip()
            if token in mass:
                mass[token] += math.exp(candidate["logprob"])
        floor = math.exp(min(candidate["logprob"] for candidate in candidates))
        weights = [mass[letter] or floor for letter in letters]
        total = sum(weights)
        if total <= 0:
            return [1 / len(letters)] * len(letters)
        return [weight / total for weight in weights]

    def _answer(self, question: QuestionLike, probabilities: list[float]) -> Answer:
        if question.type == "choice":
            by_option = dict(zip(question.options, probabilities, strict=True))
            choice = max(by_option, key=lambda option: by_option[option])
            return ChoiceAnswer(choice=choice, probabilities=by_option)
        if question.type == "score":
            score = sum(level * p for level, p in enumerate(probabilities))
            return ScoreAnswer(score=score, probabilities=probabilities)
        return CheckAnswer(probability=probabilities[0])


class GoogleLive:
    def __init__(self, model: str = "gemini-3.8-live", voice: str = "Kore") -> None:
        self.model = model
        self.voice = voice
        self.client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])

    async def open(
        self, *, tools: list[ToolSpec], instructions: str | None
    ) -> "GoogleLiveConnection":
        declarations = [
            types.FunctionDeclaration(
                name=spec.name,
                description=spec.description,
                parameters_json_schema=spec.input_schema,
                behavior=types.Behavior.BLOCKING,
            )
            for spec in tools
        ]
        config = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            system_instruction=instructions,
            tools=[types.Tool(function_declarations=declarations)] if declarations else None,
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice)
                )
            ),
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow()
            ),
        )
        connection = GoogleLiveConnection(self.client, self.model, config)
        await connection.connect()
        return connection


class GoogleLiveConnection:
    def __init__(self, client: Any, model: str, config: types.LiveConnectConfig) -> None:
        self.client = client
        self.model = model
        self.config = config
        self.context: Any = None
        self.session: Any = None
        self.handle: str | None = None
        self.resuming: str | None = None
        self.resumed_at = 0.0
        self.retries = 0
        self.leaving = False
        self.ready = asyncio.Event()
        self.idle = asyncio.Event()
        self.names: dict[str, str] = {}
        self.responding = False
        self.heard = ""

    async def connect(self) -> None:
        self.ready.clear()
        if self.context is not None:
            with contextlib.suppress(Exception):
                await self.context.__aexit__(None, None, None)
        resumption = types.SessionResumptionConfig(handle=self.handle)
        config = self.config.model_copy(update={"session_resumption": resumption})
        context = self.client.aio.live.connect(model=self.model, config=config)
        self.session = await context.__aenter__()
        self.context, self.leaving = context, False
        self.idle.set()
        self.ready.set()

    async def send_audio(self, frame: AudioFrame) -> None:
        blob = types.Blob(data=frame.data, mime_type=f"audio/pcm;rate={frame.sample_rate}")
        await self._send(lambda session: session.send_realtime_input(audio=blob))

    async def send_image(self, image: Image) -> None:
        blob = types.Blob(data=image.data, mime_type="image/jpeg")
        await self._send(lambda session: session.send_realtime_input(video=blob))

    async def send_text(self, text: str, *, respond: bool) -> None:
        turn = types.Content(role="user", parts=[types.Part(text=text)])
        if respond:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.idle.wait(), 30)
            self.idle.clear()
        await self._send(
            lambda session: session.send_client_content(turns=turn, turn_complete=respond)
        )

    async def say(self, text: str) -> None:
        await self.send_text(f"Say exactly this, nothing else: {text}", respond=True)

    async def interrupt(self, played_ms: int) -> None:
        raise NotImplementedError(
            "GoogleLive: the vendor cuts its own context when it hears the person"
        )

    async def submit_result(self, call_id: str, result: Content, is_error: bool) -> None:
        if call_id not in self.names:
            return
        text = (
            result
            if isinstance(result, str)
            else " ".join(p.text for p in result if hasattr(p, "text"))
        )
        response = {"error": text} if is_error else {"result": text}
        answer = types.FunctionResponse(id=call_id, name=self.names.pop(call_id), response=response)
        await self._send(lambda session: session.send_tool_response(function_responses=[answer]))

    async def events(self) -> AsyncIterator[LiveEvent]:
        while True:
            try:
                async for message in self.session.receive():
                    for event in self._events(message):
                        yield event
                    if self.leaving and self._quiet():
                        break
                if not (self.leaving and self._quiet()):
                    continue
                self.retries = 0
            except Exception as exc:
                if self._shaky(exc):
                    self.retries += 1
                    self.handle = self.resuming
                elif not self.leaving or self.handle is None:
                    yield LiveFailed(message=str(exc))
                    return
                else:
                    self.retries = 0
            try:
                await self._resume()
            except Exception as exc:
                yield LiveFailed(message=str(exc))
                return
            self.names.clear()
            if self.responding:
                self.responding = False
                yield LiveDone()

    async def close(self) -> None:
        await self.context.__aexit__(None, None, None)

    async def _send(self, send: Callable[[Any], Awaitable[None]]) -> None:
        await self.ready.wait()
        session = self.session
        try:
            await send(session)
        except Exception:
            if self.session is session and self.ready.is_set():
                raise
            await self.ready.wait()
            await send(self.session)

    async def _resume(self) -> None:
        self.resuming = self.handle
        while True:
            try:
                await self.connect()
            except Exception as exc:
                if getattr(exc, "code", None) != 1011 or self.retries >= 2:
                    raise
                self.retries += 1
                continue
            self.resumed_at = time.monotonic()
            return

    def _shaky(self, exc: Exception) -> bool:
        recent = time.monotonic() - self.resumed_at < 1
        return getattr(exc, "code", None) == 1011 and recent and self.retries < 2

    def _quiet(self) -> bool:
        return not self.responding and not self.names

    def _end_heard(self, events: list[LiveEvent]) -> None:
        if self.heard.strip():
            events.append(LiveHeard(text=self.heard.strip(), final=True))
        self.heard = ""

    def _events(self, message: Any) -> list[LiveEvent]:
        events: list[LiveEvent] = []
        update = message.session_resumption_update
        if update is not None and update.resumable and update.new_handle:
            self.handle = update.new_handle
        if message.go_away is not None:
            self.leaving = True
        if message.tool_call_cancellation is not None:
            for call_id in message.tool_call_cancellation.ids or []:
                self.names.pop(call_id, None)
        content = message.server_content
        if message.tool_call is not None:
            self._end_heard(events)
            for call in message.tool_call.function_calls or []:
                self.names[call.id] = call.name
                events.append(
                    LiveToolCall(call_id=call.id, name=call.name, args=dict(call.args or {}))
                )
        if content is None:
            return events
        if content.input_transcription is not None and content.input_transcription.text:
            piece = content.input_transcription.text
            apart = self.heard and not self.heard[-1].isspace() and not piece[0].isspace()
            self.heard += " " + piece if apart else piece
            events.append(LiveHeard(text=self.heard, final=False))
        speaking = content.output_transcription is not None or content.model_turn is not None
        if speaking and not self.responding:
            self.responding = True
            self.idle.clear()
            self._end_heard(events)
            events.append(LiveStarted())
        if content.output_transcription is not None and content.output_transcription.text:
            events.append(LiveSaid(text=content.output_transcription.text))
        for part in (
            content.model_turn.parts if content.model_turn and content.model_turn.parts else []
        ):
            if part.inline_data is not None and part.inline_data.data:
                rate = int(
                    re.search(r"rate=(\d+)", part.inline_data.mime_type or "rate=24000").group(1)
                )
                events.append(
                    LiveAudio(frame=AudioFrame(data=part.inline_data.data, sample_rate=rate))
                )
        if content.interrupted:
            events.append(LiveInterrupted())
        if content.interrupted or content.turn_complete:
            self.idle.set()
        if (content.interrupted or content.turn_complete) and self.responding:
            self.responding = False
            events.append(LiveDone())
        return events


def decision() -> TypeSafeProvider | LocalDecisionProvider:
    if model := os.environ.get("LOCAL_DECISION_MODEL"):
        return LocalDecisionProvider(model)
    return TypeSafeProvider()
