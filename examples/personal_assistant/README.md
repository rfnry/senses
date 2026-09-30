# personal_assistant

## Description

Jordan's personal assistant, spoken to with the camera on. The one feature is two minds: a live model that hears, sees and speaks natively holds the conversation, and asks an engine agent behind it through a `consult` tool, which can `look` at the camera and `inform` the voice. Look at `Session(link, live=, video=True, instructions=)`, `SensesTools(session, triggered_by=)` and `session.start(tools=consult)` in `src/server.py`, and `engine.tool(..., tools=senses.only("look", "inform"))` in `src/harness.py`.

## Running

```bash
export ANTHROPIC_API_KEY=...
export GOOGLE_API_KEY=...
npm --prefix ../../packages/typescript install
npm --prefix ../../packages/typescript run build
uv run src/main.py
```

## Example

Open http://localhost:8000
