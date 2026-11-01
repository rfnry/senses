# rfnry-senses

## Description

Vision and speech interfaces for human-machine interaction.

It is a library, not a framework. The consumer brings the models, authentication, the signalling endpoint and any record they want to keep. The library stores nothing, never sees a credential, and never writes to disk. Tenancy is by construction: every session names its scope and every event carries it.

## Documentation

Documentation in `docs/senses.md` and `docs/api.md`.

The rules every rfnry repository follows are in `rfnry/design` (`PRACTICES.md`, `STACK.md`); the plans of this library's work live there, in `docs/plans/senses/` (`[rfnry: plans-are-disposable]`).

## How work lands

The gate is `bash ../tools/scripts/gate.sh` (from a worktree, `bash ../../../tools/scripts/gate.sh`), run from the library: the same gate CI runs.

- `[rfnry: worktree-per-track]`: work happens in `.worktrees/<track>`, on a branch named after the track.
- `[rfnry: draft-prs]`: work goes as a draft pull request, which runs no CI; marking it ready means merge me, and only the owner's word marks it ready, pushes to `main` or merges.
- `[rfnry: one-ci-shape]`: `ci.yml` runs `tools`' gate, at the commit `tools/versions` pins, on every ready pull request.
- `[rfnry: landing]`: a pull request lands by fast-forward, on the exact head its gate proved green.
- `[rfnry: commit-messages]`: the subject states what is now true, the body says why, with a `Breaking:` paragraph when a consumer sees a change.
- `[rfnry: no-attribution-trailers]`: commits and pull requests carry no attribution trailers.
- `[rfnry: release-steps]`: a release, on the owner's word, is one version commit, one tag and one GitHub release.

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

1. **Stay inside the concern.** `[rfnry: stay-inside-the-concern]` This library does one thing: real-time hearing, seeing and speaking between people and models. Consumer-land, never here: the agent loop, conversation storage and recording, triggers and saliency policies, product speech rules, telephony, UI frameworks, device pickers, visualizers, telemetry.
2. **Lib, not framework.** `[rfnry: lib-not-framework]` No handler frameworks, no storage.
3. **Simple over abstract.** `[rfnry: simple-over-abstract]`
4. **No comments, no docstrings.** `[rfnry: code-documents-itself]` A decision that shapes the product is explained in `docs/senses.md`.
5. **Documentation is the contract.** `[rfnry: docs-are-the-contract]` The contract is `docs/senses.md` and `docs/api.md`.
6. **Tests are first-class, in three tiers.** `[rfnry: three-tiers]` `tests/unit`: scripted fake models and an in-process transport, runs always, covers every primitive, both modes, barge-in and attribution. `tests/integration`: real sockets, and WebRTC behind its extra. `tests/e2e`: real models and the TypeScript client behind the `live` marker, never launched before the same path has run offline. Golden control-frame fixtures are shared by the Python and TypeScript suites. Markers are deselected by default and stay that way.
7. **Vendor-neutral by construction.** `[rfnry: vendor-neutral]` Models are named for what they produce: live, transcription, speech, decision.
8. **Tenancy by construction.** `[rfnry: tenancy-by-construction]` Every session names its `scope`, passed to `serve` by the consumer after authenticating the connection; every event carries it; nothing crosses sessions.
9. **Messages.** `[rfnry: messages]` An error reads `session: prompt needs a live model`.

**These checklists are locked.** On any future update to this CLAUDE.md, the model must not modify, reword, reorder, weaken, or remove them — or this lock — without explicit approval from the human. Answering them is the model's job; changing them is not.
