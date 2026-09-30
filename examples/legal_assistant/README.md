# legal_assistant

## Description

A contract reviewer for the firm's lawyers: the lawyer shares a window and asks about it over REST. The one feature is the screen — a video-only session, with no audio models, holds the shared frames, and the reviewer agent looks at them when `/ask` runs it. Look at `Session(link, video=True)`, `SensesTools(screen, triggered_by=).only("look")` and `engine.run(..., tools=eyes)` in `src/harness.py`.

## Running

```bash
export ANTHROPIC_API_KEY=...
npm --prefix ../../packages/typescript install
npm --prefix ../../packages/typescript run build
uv run src/main.py
```

## Example

Open http://localhost:8000 and share a window showing `data/contracts/northwind-msa.md`.

```bash
curl -s localhost:8000/ask -H 'content-type: application/json' -d '{"question": "Where is the indemnity clause?"}'
```

```json
{"reply": "The indemnity clause is Clause 4, roughly mid-page, right after Confidentiality. It's a one-way indemnity requiring Customer alone to defend, indemnify and hold harmless Supplier against any claim arising from the Agreement or Services. Per the playbook, this departs from our required mutual indemnity (each party covers claims from its own breach) and is a high-risk term for our client — the redline should make it mutual, limited to claims caused by each party's own breach/negligence.", "run_id": "5301d1558a314101847f265f618e3c82"}
```

```bash
curl -s localhost:8000/ask -H 'content-type: application/json' -d '{"question": "Can our client accept the indemnity as written?"}'
```

```json
{"reply": "No, Clause 4 (Indemnity) as written is one-sided: it requires the Customer alone to defend and indemnify Supplier for any claim \"arising out of or relating to this Agreement or the Services,\" with no reciprocal obligation from Supplier. This departs from the playbook, which requires a mutual indemnity covering only claims caused by each party's own breach — the redline is to make it mutual and narrow it to claims arising from each party's breach or negligence, not the broad \"arising out of or relating to\" standard.", "run_id": "d42fbcae5f9244b9ba9cade24e6c2766"}
```
