# GitHub App automation for generated Geas skills

Geas pull requests run generation and tests with read-only repository authority.
A separate workflow, loaded only from protected `refs/heads/main`, may write back
the two manifest-bound skill snapshots and request auto-merge. This arrangement
is optional. Local-only publication and capability-gated direct push do not
depend on the App.

## Fixed identities and permissions

The organization is `Epiphytic` (GitHub organization ID `228616596`). Install
the GitHub App on both selected repositories: the publication target
`Epiphytic/geas` (repository ID `1320458746`, repository name `geas`) and the
protected identity-policy repository `Epiphytic/.github`. The STS identity below
still issues a token scoped only to repository `geas`; access to `.github` lets
the STS deployment resolve the protected policy without organization-wide
repository access. Give the installation these repository permissions:

- Contents: read and write, for the exact manifest-owned skill write-back and
  the eventual merge.
- Pull requests: read and write, for App approval and enabling auto-merge.

Do not grant Actions, Checks, Administration, Members, Secrets, Workflows, or
organization-wide repository access to this identity. The protected workflow's
ordinary `GITHUB_TOKEN`, not the App token, performs Actions, metadata,
contents, and pull-request reads.

Deploy `octo-sts-rust` at the operator-controlled STS endpoint and place this
identity policy in the protected organization repository at
`Epiphytic/.github:.github/chainguard/geas-pr-skill-sync.sts.yaml`:

```yaml
issuer: https://token.actions.githubusercontent.com
subject: repo:Epiphytic@228616596/geas@1320458746:ref:refs/heads/main
claim_pattern:
  repository_id: '1320458746'
  event_name: workflow_run
  workflow_ref: 'Epiphytic/geas/\.github/workflows/pr-skill-writeback\.yml@refs/heads/main'
permissions:
  contents: write
  pull_requests: write
repositories:
  - geas
```

The workflow exchanges this identity as scope `Epiphytic/.github`, identity
`geas-pr-skill-sync`, through `sts.epiphytic.org`. Keep the organization and
repository numeric IDs pinned: names alone are reusable after deletion or
transfer. Do not broaden the subject to pull-request refs, tags, or arbitrary
workflow revisions.

## Protected workflow sequence

`.github/workflows/pr-skill-regeneration.yml` runs on the exact pull-request
head with `contents: read`, no OIDC permission, no secret, and checkout
credentials disabled. It builds a closed artifact containing only:

- `.agents/skills/geas/**`
- `.agents/skills/open-source-research-agents/**`

`.github/workflows/pr-skill-writeback.yml` is triggered by `workflow_run` and
checks out trusted code from `refs/heads/main`. Before requesting the App token,
it fetches the protected identity policy at `refs/heads/main`, runs the trusted
`validate-policy` CLI command, and performs all other initial reads with the
ordinary read-scoped token. It rejects:

- a fork, failed run, wrong repository ID, workflow name/path/event, or PR;
- a closed, retargeted, renamed, or advanced pull request;
- a mismatched artifact source, inventory, path, size, hash, mode, or snapshot;
- a symlink, extra file or directory, unsafe path, or unexpected Git tree; and
- a broadened STS subject, claim, permission, repository, or App identity.

The PR is re-queried and the exact head is revalidated immediately before OIDC
exchange. Each GitHub file listing is bracketed by two exact PR-state reads and
must equal a local, rename-disabled Git diff of the verified base and head
commits; this prevents a stale or ABA file response from hiding an unsafe path.
The complete inventory is fetched and rebound immediately before exchange and
again after write-back. After exchange, protected code copies inert,
independently verified bytes and pushes only the two roots with
`--force-with-lease=refs/heads/<head>:<expected-sha>`. PR-controlled Python,
shell, hooks, filters, and build commands never run with the App token. In
particular, do not replace this design with `pull_request_target` checkout or
execution.

The workflow then re-queries the PR before App approval and again before
auto-merge. The approval request includes `commit_id` for the exact verified
post-writeback head, and protected code validates the returned review's commit
and PR identity. Auto-merge uses that same head SHA and remains subject to
repository rulesets and required checks. Knowledge promotions,
source cards, accepted ontology records, and policy files are not eligible for
this default artifact workflow; they additionally require an effective local
`knowledge.auto_promote` grant and successful existing promotion verification.

## Rulesets and audit checks

Protect `main` with a repository ruleset that rejects direct updates by this
workflow, requires pull requests, requires the `PR Skill Regeneration / generate`
check, requires the configured independent App approval, and requires branches
to be current before merge. Enable squash auto-merge and prevent workflow files
or organization identity policy from bypassing review. The App must be distinct
from the identity that authored the PR.

For each run, verify the `workflow_run` source, immutable action SHAs, OIDC
subject, repository ID, App installation repository selection, artifact digest,
exact lease, approving actor, expected head, and merge result. A fork PR may run
the read-only regeneration workflow, but protected write-back, token exchange,
approval, and auto-merge are skipped.

## Rotation and removal

Rotate the GitHub App private material and the STS deployment signing or trust
material according to the operator's credential policy. Rotation must preserve
the exact App installation selection and policy claims; validate the policy and
exercise a same-repository test PR before restoring unattended auto-merge.

To remove automation, disable the write-back workflow first, remove the
`geas-pr-skill-sync` STS identity, revoke the App installation from both `geas`
and `.github`, and then delete unused App credentials and deployment
configuration. Confirm that no queued `workflow_run` can exchange a token.
Leave branch protections and read-only PR checks in place unless the repository
owner separately approves their removal.
