# Repository lifecycle

The local snapshot remains usable without Geas. Geas is optional and this skill never installs or configures it.

## Local lifecycle

- Install and inspect locally: `geas repository-install open-source-research-agents https://github.com/Epiphytic/geas.git --ref refs/heads/main --read-only --publish none`.
- Refresh without publishing: `geas repository-update open-source-research-agents --publish none`.
- Remove only receipt-owned state: `geas repository-remove open-source-research-agents`.

## Publication authority

Before a first remote or current-repository install may publish, configure an exact-repository, exact-ref root-local `git.pull_request` grant for `https://github.com/Epiphytic/geas.git` and `refs/heads/main`, or root-local `git.direct_push` plus explicit `--direct-push`.
The initially unknown generated manifest requires subject paths: `"*"` and bundle_sha256: `"*"`. These selectors authorize only that Git capability; they do not grant repository read, source access, model use, promotion, or any other Git capability.
For narrower authority, keep the install at `--publish none`, inspect its verified JSON receipt and every complete generated skill manifest, add exact local grants for each receipt-owned leaf and producer bundle, then run `geas repository-update open-source-research-agents`. Pull request is the default; direct push remains separately explicit.
See the generic Geas skill's `references/cli.md` or the [repository guide](https://github.com/Epiphytic/geas/blob/main/docs/REPOSITORY_ONTOLOGIES.md) for the complete grant shape and ambiguity fallback.
