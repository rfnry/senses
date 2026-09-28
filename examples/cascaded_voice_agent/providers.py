import asyncio
import base64
import contextlib
import json
import os
import re
from array import array
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import websockets
from anthropic import AsyncAnthropic
from anthropic.types import Message as AnthropicMessage
from google import genai
from google.genai import types
from rfnry_engine import (
    Audio,
    Content,
    Document,
    Image,
    Message,
    Part,
    Reply,
    Text,
    ToolCall,
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
    Transcript,
    UtteranceEnd,
)


class AnthropicProvider:
    media_types = {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "application/pdf",
        "text/*",
    }
    stops = {
        "end_turn": "end_turn",
        "stop_sequence": "end_turn",
        "tool_use": "tool_use",
        "max_tokens": "max_tokens",
    }

    def __init__(
        self, model: str = "claude-sonnet-5", max_tokens: int = 4096, effort: str = "low"
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self.client = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=3)

    async def generate(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> Reply:
        response = await self.client.messages.create(**self._request(system, messages, tools))
        return self._reply(response)

    async def generate_stream(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        on_delta: Callable[[str], Awaitable[None]],
    ) -> Reply:
        async with self.client.messages.stream(**self._request(system, messages, tools)) as stream:
            async for text in stream.text_stream:
                await on_delta(text)
            return self._reply(await stream.get_final_message())

    def _request(
        self, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": self._turns(messages),
            "tools": [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ],
            "output_config": {"effort": self.effort},
        }

    def _turns(self, messages: list[Message]) -> list[dict[str, Any]]:
        turns: list[dict[str, Any]] = []
        for message in messages:
            content: list[dict[str, Any]]
            if message.role == "tool":
                result = {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content
                    if isinstance(message.content, str)
                    else self._blocks(message.content),
                }
                role, content = "user", [result | ({"is_error": True} if message.is_error else {})]
            elif message.role == "assistant" and message.reasoning:
                role, content = "assistant", message.reasoning
            elif message.role == "assistant":
                calls = [
                    {"type": "tool_use", "id": c.id, "name": c.name, "input": c.args}
                    for c in message.tool_calls
                ]
                role, content = "assistant", self._blocks(message.content) + calls
            else:
                role, content = "user", self._blocks(message.content)
            if turns and turns[-1]["role"] == role:
                turns[-1]["content"] = turns[-1]["content"] + content
            else:
                turns.append({"role": role, "content": content})
        return turns

    def _blocks(self, content: Content) -> list[dict[str, Any]]:
        if isinstance(content, str):
            return [{"type": "text", "text": content}] if content else []
        return [self._block(part) for part in content]

    def _block(self, part: Part) -> dict[str, Any]:
        if isinstance(part, Text):
            return {"type": "text", "text": part.text}
        if isinstance(part, Audio):
            raise ValueError("anthropic: audio input is not supported")
        if part.url is not None:
            source = {"type": "url", "url": part.url}
        elif isinstance(part, Document) and part.media_type.startswith("text/"):
            source = {
                "type": "text",
                "media_type": "text/plain",
                "data": (part.data or b"").decode(),
            }
        else:
            data = base64.b64encode(part.data or b"").decode()
            source = {"type": "base64", "media_type": part.media_type, "data": data}
        block = {"type": part.type, "source": source}
        return block | ({"title": part.name} if isinstance(part, Document) and part.name else {})

    def _reply(self, response: AnthropicMessage) -> Reply:
        blocks = [block.model_dump(exclude_none=True) for block in response.content]
        thinking = any(block["type"] in ("thinking", "redacted_thinking") for block in blocks)
        usage = response.usage
        return Reply(
            text="".join(block["text"] for block in blocks if block["type"] == "text"),
            tool_calls=[
                ToolCall(id=b["id"], name=b["name"], args=b["input"])
                for b in blocks
                if b["type"] == "tool_use"
            ],
            usage=Usage(
                input=usage.input_tokens,
                output=usage.output_tokens,
                cache_creation=usage.cache_creation_input_tokens or 0,
                cache_read=usage.cache_read_input_tokens or 0,
            ),
            stop_reason=self.stops.get(response.stop_reason or "", "error"),
            model=response.model,
            reasoning=blocks if thinking else None,
        )


class OpenAIProvider:
    url = "wss://api.openai.com/v1/realtime?intent=transcription"
    rate = 24000

    def __init__(self, model: str = "gpt-transcribe", eagerness: str = "auto") -> None:
        self.model = model
        self.eagerness = eagerness
        self.key = os.environ["OPENAI_API_KEY"]

    def stream(self) -> "OpenAIStream":
        return OpenAIStream(self)


class OpenAIStream:
    def __init__(self, provider: OpenAIProvider) -> None:
        self.provider = provider
        self.socket: Any = None
        self.opening = asyncio.Lock()

    async def feed(self, frame: AudioFrame) -> None:
        socket = await self._open()
        pcm = self._resample(frame, self.provider.rate)
        await socket.send(
            json.dumps(
                {"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()}
            )
        )

    async def events(self) -> AsyncIterator[Transcript | UtteranceEnd]:
        socket = await self._open()
        running: dict[str, str] = {}
        async for message in socket:
            event = json.loads(message)
            kind = event["type"]
            if kind == "conversation.item.input_audio_transcription.delta":
                running[event["item_id"]] = running.get(event["item_id"], "") + event["delta"]
                yield Transcript(text=running[event["item_id"]], final=False)
            elif kind == "conversation.item.input_audio_transcription.completed":
                running.pop(event["item_id"], None)
                yield Transcript(text=event["transcript"], final=True)
                yield UtteranceEnd()
            elif kind == "error":
                raise RuntimeError(f"OpenAIProvider: {event['error'].get('message')}")

    async def close(self) -> None:
        if self.socket is not None:
            await self.socket.close()

    async def _open(self) -> Any:
        async with self.opening:
            if self.socket is None:
                self.socket = await websockets.connect(
                    self.provider.url,
                    additional_headers={"Authorization": f"Bearer {self.provider.key}"},
                    max_size=None,
                )
                await self.socket.send(json.dumps(self._session()))
        return self.socket

    def _session(self) -> dict[str, Any]:
        audio = {
            "format": {"type": "audio/pcm", "rate": self.provider.rate},
            "transcription": {"model": self.provider.model},
            "turn_detection": {"type": "semantic_vad", "eagerness": self.provider.eagerness},
        }
        return {
            "type": "session.update",
            "session": {"type": "transcription", "audio": {"input": audio}},
        }

    def _resample(self, frame: AudioFrame, rate: int) -> bytes:
        samples = array("h", frame.data)
        if frame.channels == 2:
            samples = array(
                "h", [(a + b) // 2 for a, b in zip(samples[::2], samples[1::2], strict=True)]
            )
        if frame.sample_rate == rate or not samples:
            return samples.tobytes()
        count = len(samples) * rate // frame.sample_rate
        step = frame.sample_rate / rate
        out = array("h", bytes(2 * count))
        for i in range(count):
            position = i * step
            low = int(position)
            high = min(low + 1, len(samples) - 1)
            weight = position - low
            out[i] = int(samples[low] * (1 - weight) + samples[high] * weight)
        return out.tobytes()


class GoogleSpeech:
    def __init__(self, model: str = "gemini-3.1-flash-tts-preview", voice: str = "Kore") -> None:
        self.model = model
        self.voice = voice
        self.client = genai.Client(
            api_key=os.environ["GOOGLE_API_KEY"],
            http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=4)),
        )

    async def speak(self, text: str) -> AsyncIterator[AudioFrame]:
        response = await self.client.aio.models.generate_content(
            model=self.model,
            contents=text,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice)
                    )
                ),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
        parts = response.candidates[0].content.parts if response.candidates else []
        audio = [part.inline_data for part in parts or [] if part.inline_data is not None]
        if not audio:
            raise RuntimeError("GoogleSpeech: the model returned no audio")
        rate = int(re.search(r"rate=(\d+)", audio[0].mime_type or "").group(1))
        data = b"".join(blob.data for blob in audio)
        step = rate // 10 * 2
        for start in range(0, len(data) - len(data) % 2, step):
            yield AudioFrame(data=data[start : start + step], sample_rate=rate)


class GoogleLive:
    def __init__(self, model: str = "gemini-3.1-flash-live-preview", voice: str = "Kore") -> None:
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
        self.leaving = False
        self.names: dict[str, str] = {}
        self.responding = False
        self.heard = ""

    async def connect(self) -> None:
        resumption = types.SessionResumptionConfig(handle=self.handle)
        config = self.config.model_copy(update={"session_resumption": resumption})
        context = self.client.aio.live.connect(model=self.model, config=config)
        session = await context.__aenter__()
        previous = self.context
        self.context, self.session, self.leaving = context, session, False
        if previous is not None:
            with contextlib.suppress(Exception):
                await previous.__aexit__(None, None, None)

    async def send_audio(self, frame: AudioFrame) -> None:
        blob = types.Blob(data=frame.data, mime_type=f"audio/pcm;rate={frame.sample_rate}")
        await self._send(lambda session: session.send_realtime_input(audio=blob))

    async def send_image(self, image: Image) -> None:
        blob = types.Blob(data=image.data, mime_type="image/jpeg")
        await self._send(lambda session: session.send_realtime_input(video=blob))

    async def send_text(self, text: str, *, respond: bool) -> None:
        turn = types.Content(role="user", parts=[types.Part(text=text)])
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
            except Exception as exc:
                if not self.leaving or self.handle is None:
                    yield LiveFailed(message=str(exc))
                    return
            try:
                await self.connect()
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
        session = self.session
        try:
            await send(session)
        except Exception:
            if self.session is session:
                raise
            await send(self.session)

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
        if (content.interrupted or content.turn_complete) and self.responding:
            self.responding = False
            events.append(LiveDone())
        return events
