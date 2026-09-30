## Running

```bash
export ANTHROPIC_API_KEY=...
export DECISION_API_KEY=...
# optional: export LOCAL_DECISION_MODEL=llama3.1:8b
npm --prefix ../../packages/typescript install
npm --prefix ../../packages/typescript run build
uv run main.py
```

## Example

Open http://localhost:8000 and share a window showing `data/contracts/northwind-msa.md`.

```bash
curl -s localhost:8000/ask -H 'content-type: application/json' -H 'X-Tenant: firm-acme' -d '{"question": "Where is the indemnity clause?"}'
```

```json
{"reply": "The indemnity clause is Clause 4, \"Indemnity,\" in the middle of the page, right below Confidentiality (Clause 3) and above Limitation of Liability (Clause 5). It reads that Customer shall defend, indemnify and hold harmless Supplier against any claim, loss or expense arising out of or relating to the Agreement or the Services.", "run_id": "a90641a043d04dc08ad2b981f6dd0dc9"}
```

```bash
curl -s localhost:8000/ask -H 'content-type: application/json' -H 'X-Tenant: firm-acme' -d '{"question": "Can our client accept the indemnity as written?"}'
```

```json
{"reply": "No — Clause 4 (Indemnity, middle of the page) is unacceptable as written. It's a one-way indemnity running only from Customer (the client) to Supplier, covering any claim \"arising out of or relating to this Agreement or the Services\" with no carve-out for Supplier's own fault, whereas the playbook requires a mutual indemnity where each party covers claims caused by its own breach. This is a severe departure from that position and should be revised to mutual, fault-based indemnification before the client signs.", "run_id": "06b101c5412f4622b6f823de0cace21c"}
```
