# factory_assistant

## Description

Acme Plastics' maintenance assistant, spoken to by a technician on the floor with the camera on: a cascaded session around the engine's maintenance agent. The one feature is seeing — the agent looks through the camera only when it has a reason to, with the `look` tool. Look at `Session(..., video=True)` and `SensesTools(session, triggered_by=)` in `src/server.py`, and `engine.run(..., tools=senses.only("look"))` in `src/harness.py`.

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
