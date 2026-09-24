# Examples

Each folder is a standalone project: a `main.py` that reads top to bottom, a `providers.py` with the
vendors that example uses, its own `agents/` tree when an engine agent takes part, and a
`pyproject.toml` that shows how few dependencies it needs. Every vendor class is the same, byte for
byte, in every folder that carries it; `AnthropicProvider` and `TypeSafeProvider` are rfnry-engine's
own examples' classes.

| example | what it shows | vendors |
| --- | --- | --- |
| `barge_in` | offline, no keys: a `Loopback` and scripted models; a `say` cut at 1.2 s by the person talking, and what was heard when the client played 0.4 s, 1.2 s, or never reported (the clock estimate) | none |
| `cascaded_voice_agent` | the Getting started endpoint for real: a FastAPI WebSocket authenticated on the upgrade (a cookie and an `Origin` check), `serve`, a cascaded session with a turn check, an engine agent answering each `TurnEnded`, and the browser page with captions from `subscribe` | OpenAI transcription, Google speech, Anthropic agent, decision turns |
| `live_voice_consult` | a live session whose tool is `engine.tool("core", …, name="consult")`; `ToolCalled` printed; `inform` pushing a fact mid-call, then `prompt` | Google live, Anthropic core agent |
| `camera_look` | a camera-only session; an engine agent with `SensesTools` (`look` only) answering questions about what the camera sees; in the browser, JPEG over a WebSocket at 1 frame a second | Anthropic agent |
| `two_minds_thread` | a live voice in front and an engine agent behind through `SensesTools`, the thread as the transcript both read (`ThreadTools`), and the `mirror` glue from the docs posting `Heard` and `Said` into it | Google live, Anthropic agents, rfnry-thread |
| `webrtc_voice` | `live_voice_consult` over WebRTC: the offer endpoint authenticated with a bearer header or a cookie, `accept_offer`, and the browser on `transport: 'webrtc'` | Google live, Anthropic core agent |

| provider | contract | how |
| --- | --- | --- |
| `OpenAIProvider` | `TranscriptionModel` | streaming realtime transcription: a Realtime transcription session over a WebSocket with `gpt-transcribe` and server turn detection; the running text arrives as partial `Transcript`s once the turn is committed, then the final one and `UtteranceEnd`; 16 kHz audio is resampled to 24 kHz in the adapter |
| `GoogleSpeech` | `SpeechModel` | Gemini text-to-speech, one request per sentence, 24 kHz PCM |
| `GoogleLive` | `LiveModel` | the Gemini Live API: audio and camera frames in, voice and both transcripts out, tool calls answered; `say` asks for the exact words; `interrupt` raises, as the vendor cuts its own context when it hears the person |
| `AnthropicProvider` | generation | the engine agents' text mind |
| `TypeSafeProvider` | `DecisionModel` | the turn check |

## Running

The keys live outside the repository, in a file with `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`,
`OPENAI_API_KEY` and `DECISION_API_KEY`. Run an example from inside its folder:

```bash
cd examples/live_voice_consult
uv run --env-file ../../../.env.examples main.py
```

Every example runs headless from a terminal: the person's words are made with the speech model at
runtime and fed through a `Loopback` (or, in `webrtc_voice`, an in-process peer connection). The
browser examples also serve a page: build the client once (`cd packages/typescript && npm run
build`), then run `main.py browser` and open `http://localhost:8000`.

`uv` builds each folder's environment on first run; the per-folder `uv.lock` files are ignored by
git. Lint an example with its own ruff: `uv run ruff format . && uv run ruff check .`.
