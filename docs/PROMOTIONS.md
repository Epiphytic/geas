# Git-native proposal promotion

Model extraction is proposal-only. Promotion uses an inspectable Git artifact
instead of a model judgment or forge API decision.

## Authority boundary

`promotion-stage` renders one immutable extraction proposal into a canonical
JSON manifest. The manifest includes the full proposal and candidate knowledge
pack, and binds:

- the immutable proposal ID and canonical record SHA-256;
- the source blob, structural derivation, anchor IDs, exact character ranges,
  and quote hashes;
- the target canonical branch and its base commit;
- the exact accepted pack SHA-256 and deterministic renderer version;
- the rule that repository approval policy is out of scope.

Staging does not write accepted records. Opening or approving a GitHub PR,
GitLab MR, or Radicle patch does not write accepted records either.

`promotion-verify` reads the file with `git show` from the declared canonical
local branch. It rejects an absent file, a target-ref mismatch, a base commit
that is not an ancestor, a missing immutable proposal, changed proposal data,
changed evidence, invalid ranges, or a pack that is not a lossless rendering.
It does not invoke a model, shell-expand source text, execute manifest content,
or consult a forge API.

`promotion-apply` performs that same verification and then deterministically
materializes accepted immutable records. Reapplying the same canonical
promotion is idempotent because both ontology records and the receipt are
content-addressed.

Repository rules decide who or what may merge a commit. Required reviews,
branch protection, Radicle delegates, CI policies, automatic approval, and
automatic merge are intentionally outside this application.

## CLI workflow

Start on a topic branch after creating an extraction proposal:

```bash
uv run geas promotion-stage \
  extraction-proposal:sha256:... \
  --topic "Open source research agents" \
  --topic-concept-id concept:open-source-research-agents \
  --output ontology/promotions/example.json \
  --root data
```

The receipt contains argument arrays for GitHub, GitLab, and Radicle. They are
display-only transport hints; the command does not execute or publish them.
Commit the manifest and submit the branch through the desired review mechanism.

After the exact change reaches the local canonical branch:

```bash
uv run geas promotion-verify \
  ontology/promotions/example.json \
  --root data

uv run geas promotion-apply \
  ontology/promotions/example.json \
  --root data
```

The default authority is `refs/heads/main`. A deployment using another local
canonical branch passes the same full ref to staging and verification.

## Edit policy

A reviewer may request or contribute a new patch revision, but semantic content
must remain a lossless rendering of the bound extraction proposal. Corrections
to a claim, controversy, gap, concept, or exact evidence selector require a new
attributable proposal. This keeps review from becoming an untracked content
creation channel.

Exact ranged evidence is supported even when the same quotation occurs more
than once in a source. The importer verifies the selected range against the
original content-addressed blob rather than guessing an occurrence.

## Forge mappings

- GitHub: branch to pull request.
- GitLab: branch to merge request.
- Radicle: commit pushed to `HEAD:refs/patches`.

These mappings follow the official [GitHub CLI pull-request
workflow](https://cli.github.com/manual/gh_pr_create), [GitLab merge-request
workflow](https://docs.gitlab.com/user/project/merge_requests/creating_merge_requests/),
and [Radicle patch
workflow](https://radicle.xyz/guides/user/). Their review and merge semantics
remain repository policy.
