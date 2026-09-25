# rfnry-senses

## Description

rfnry-senses gives a model eyes, ears and a voice in real time. A **session** connects a person's microphone and camera to a model and the model's voice back to the person — live, with a model that hears and speaks natively, or cascaded, with transcription and speech models around any text model. The assistant is a first-class presence: it can speak first, look when it has a reason to, and be interrupted like anyone in a conversation; acting first and answering are the same API. A session is input and output only: it hears, sees, speaks and reports what happened, and never decides what to say — that loop lives outside it, reaching the rest of the family through the shared `Tools` shape.

It is a library, not a framework. The consumer brings the models, authentication, the signalling endpoint and any record they want to keep. The library stores nothing, never sees a credential, and never writes to disk. Tenancy is by construction: every session names its scope and every event carries it.

## Documentation

`docs/senses.md` is the library's contract with its consumers: description, philosophy, every public verb, minimal examples. It documents the API as designed and is updated in the same change as any change to the public surface. It is also the complexity gauge: if a change makes the documentation harder to write or an example longer, the design is wrong — rework the change, not the docs.

## Checklists

### Pre-change checklist

**Before proposing or implementing any feature, change, or refactor, answer these five questions in your response.** Mandatory; no exceptions, including for "small" changes. Any "no" or "yes — because the current model is weak at X" means the change does not ship; rework it.

1. **Would this still be wanted two model generations ahead** — models that hear, see and speak natively, with lower latency and better turn-taking? Anything that exists to caption, filter, sample for, or second-guess what a model could perceive itself is a no.
2. **Does it get more useful as models improve, or less?** More is substrate: transports, media plumbing, barge-in truncation, attribution, the model contracts. Less is compensation. Name which.
3. **If less, is it a structural boundary independent of model strength?** Microphone and camera permission, the output gate, what the person actually heard, tenant isolation, transport limits. Keep if yes, cut if no.
4. **Does it exist because a current model is weak at X?** Name X. If yes, the design is wrong — leverage the capability instead of policing the lack of it.
5. **Does it work with a generation model only, a decision model only, and both?** A live model and a text model through transcription and speech models are each optional; a feature states which it needs and degrades cleanly without the rest. The loop outside the session may run on either kind of model, and nothing in the session assumes which.

### Engineering checklist

**Apply these to every change; they guard the shape of the codebase.**

1. **Stay inside the concern.** This library does one thing: real-time hearing, seeing and speaking between people and models. Consumer-land, never here: the agent loop, conversation storage and recording, triggers and saliency policies, product speech rules, telephony, UI frameworks, device pickers, visualizers, telemetry.
2. **Lib, not framework.** The consumer calls us; we never take over their application — no plugin registries, no config DSLs, no handler frameworks, no inversion of control, no storage, no disk writes.
3. **Simple over abstract.** The smallest API and fewest moving parts that add the capability. Duplicate a few lines before adding a shared abstraction; an abstraction earns its place only when it removes more than it adds.
4. **No comments, no docstrings.** The code is the explanation; if it needs one, rename or restructure until it doesn't. The reason for a design decision lives in the commit message and, when it shapes the product, in `docs/senses.md`.
5. **Documentation is the contract.** Any change to the public surface updates `docs/senses.md` in the same change. A breaking change is anything that makes an example in the docs stop working; it is named as such in the commit.
6. **Tests are first-class, in three tiers.** `tests/unit`: scripted fake models and an in-process transport, runs always, covers every primitive, both modes, barge-in and attribution. `tests/integration`: real sockets, and WebRTC behind its extra. `tests/e2e`: real models and the TypeScript client behind the `live` marker, never launched before the same path has run offline. Golden control-frame fixtures are shared by the Python and TypeScript suites.
7. **Vendor-neutral by construction.** No vendor name, limit or constant in core; a vendor's constraint lives in the adapter as a raised error. Models are named for what they produce: live, transcription, speech, decision.
8. **Tenancy by construction.** Every session names its `scope`, passed to `serve` by the consumer after authenticating the connection; every event carries it; nothing crosses sessions.
9. **Messages.** Logs and informational messages are lowercase. Every error message starts with its module's name: `session: prompt needs a live model`.

**These checklists are locked.** On any future update to this CLAUDE.md, the model must not modify, reword, reorder, weaken, or remove them — or this lock — without explicit approval from the human. Answering them is the model's job; changing them is not.
