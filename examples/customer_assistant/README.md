# customer_assistant

## Description

Acme Home's support line, called from the browser: the engine's support agent, heard through a transcription model and voiced by a speech model — a cascaded session. The one feature is speaking a reply while it is still being written, and being cut off: `engine.run(..., on_event=)` streams its text into `session.say()`, and `said.heard`, what the customer actually heard, is what goes into the history. Look at `Session(link, transcription=, speech=)`, `TurnEnded` from `session.events()`, and `run_stream` in `src/harness.py`.

## Running

```bash
export ANTHROPIC_API_KEY=...
export GOOGLE_API_KEY=...
export OPENAI_API_KEY=...
npm --prefix ../../packages/typescript install
npm --prefix ../../packages/typescript run build
uv run src/main.py
```

## Example

Open http://localhost:8000
