# Geas Agent Skill Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build deterministic ontology-to-Agent-Skill export, safe user/repository installation and removal, trusted explicit updates, an automatically installed generic Geas skill, and offline lifecycle CI coverage.

**Architecture:** Add a focused `agent_skills` domain module that owns strict manifests, deterministic rendering, managed snapshots, agent adapters, and lifecycle operations. Add a separate `geas_update` module for trusted executable provenance and bounded self-update, leaving `cli.py` as orchestration. Package the generic skill as immutable package data, and test every state transition through temporary config homes and Git repositories.

**Tech Stack:** Python 3.12, Pydantic strict models, pathlib, subprocess/Git, importlib.metadata/resources, pytest, Ruff, GitHub Actions, Agent Skills Markdown.

**Spec:** `docs/superpowers/specs/2026-08-26-geas-agent-skill-export-design.md`

## Global Constraints

- Git ontology and policy files remain canonical; exported skills are disposable projections.
- Untrusted source/model/skill text never selects endpoints, credentials, installers, policies, approvals, budgets, tools, or canonical writes.
- Normal tests are offline and deterministic; Git and installer boundaries use temporary repositories or injected runners.
- Generated files and receipts sort unordered values; portable bytes contain no timestamps, hostnames, usernames, absolute paths, detection state, or symlink state.
- `geas-skill.json` is strict version 1 canonical JSON with one trailing newline; its inventory excludes itself and its snapshot digest hashes the ordered canonical inventory.
- Snapshot writes are sibling-temporary and atomic; managed content is replaced or removed only after strict manifest/inventory validation unless exact-scope `--force` is supplied.
- User canonical skills live at `<geas-config-root>/skills/<name>`; Codex/OpenCode share `~/.agents/skills/<name>` and Claude uses `~/.claude/skills/<name>`.
- Repository canonical skills prefer `.agents/skills/<name>` and fall back to `.geas/skills/<name>` only when Git ignores the preferred path; repository links are relative.
- Repository lifecycle commands never edit ignore files, commit, or push.
- No generated skill installs Geas; `skill-update` may update an existing trusted Geas installation only during that explicit command.
- Tests cover success, rejection, idempotence, drift, escaping links, interrupted replacement, dirty/diverged Git, and preservation of unrelated content.
- Before completion run `uv sync --extra dev`, `uv run ruff check .`, `uv run pytest -q`, and `git diff --check`.

---

### Task 1: Strict manifests and deterministic ontology skill rendering

**Files:**
- Create: `src/research_agent/agent_skills.py`
- Create: `tests/test_agent_skills.py`
- Modify: `src/research_agent/render.py`

**Interfaces:**
- Consumes: `projection.TopicView`; existing deterministic topic/Obsidian renderers.
- Produces: `SkillManifest`, `SkillFile`, `OntologyIdentity`, `GeasIdentity`, `ProjectionIdentity`; `render_ontology_skill(topic: TopicView, *, skill_name: str, ontology_name: str, repository_url: str, branch: str, ontology_commit: str, geas_version: str, geas_commit: str | None) -> dict[Path, bytes]`; `validate_snapshot(directory: Path) -> SkillManifest`.

- [ ] **Step 1: Write failing strict-manifest tests**

Add tests that instantiate the strict records, reject extra keys, absolute/traversing file paths, unsorted or duplicate inventories, invalid names/commits, an incorrect `snapshot_sha256`, and a manifest that omits or mis-hashes a generated file. Assert that a canonical manifest round-trip has sorted keys and exactly one trailing newline.

- [ ] **Step 2: Run the manifest tests and verify RED**

Run: `uv run pytest tests/test_agent_skills.py -k 'manifest or inventory' -v`

Expected: collection/import failure because `research_agent.agent_skills` does not exist.

- [ ] **Step 3: Implement strict manifest records and validation**

Use `StrictModel`, `hashlib.sha256`, normalized POSIX paths, 40-lowercase-hex Git IDs, and canonical serialization:

```python
class SkillManifest(StrictModel):
    format_version: Literal[1] = 1
    skill: SkillIdentity
    ontology: OntologyIdentity
    geas: GeasIdentity
    projection: ProjectionIdentity
    files: tuple[SkillFile, ...]
    snapshot_sha256: str

def snapshot_digest(files: tuple[SkillFile, ...]) -> str:
    payload = json.dumps(
        [item.model_dump(mode="json") for item in files],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()
```

`validate_snapshot` must refuse symlinked roots/files, inventory every regular file other than `geas-skill.json`, compare exact sorted paths and hashes, and return the validated manifest.

- [ ] **Step 4: Run manifest tests and verify GREEN**

Run: `uv run pytest tests/test_agent_skills.py -k 'manifest or inventory' -v`

Expected: all selected tests pass.

- [ ] **Step 5: Write failing renderer/determinism tests**

Construct one `TopicView` containing a parent/child concept, two claims with exact evidence selectors and original URLs, a controversy, a gap, a citation relation, and a threat. Render it twice with every tuple reversed in one input. Assert byte-identical outputs; exactly `SKILL.md`, `geas-skill.json`, and one-hop `references/index.md` plus typed pages; source URLs and record IDs remain; no full-document sentinel, absolute temp path, timestamp field, username, or hostname appears; and every link from `SKILL.md` resolves through `references/index.md`.

- [ ] **Step 6: Run renderer tests and verify RED**

Run: `uv run pytest tests/test_agent_skills.py -k 'render or deterministic or reference' -v`

Expected: failure because `render_ontology_skill` is absent.

- [ ] **Step 7: Implement concise hierarchical rendering**

Render portable frontmatter containing only `name` and `description`. Route the entrypoint to `references/index.md`; split existing Obsidian notes into typed `references/{concepts,claims,controversies,gaps,sources,citations,threats}/`; keep source URLs/evidence excerpts and provenance IDs; include repository URL, ontology name/branch/commit, optional Geas commands/project URL, and unlink/removal guidance. Convert all text to UTF-8 with one trailing newline and build the strict manifest after hashing every non-manifest file.

- [ ] **Step 8: Run all Task 1 tests, refactor, and commit**

Run: `uv run pytest tests/test_agent_skills.py -v && uv run ruff check src/research_agent/agent_skills.py src/research_agent/render.py tests/test_agent_skills.py`

Commit: `feat: render deterministic ontology skills`

---

### Task 2: Managed snapshots, agent adapters, linking, export, unlink, and removal

**Files:**
- Modify: `src/research_agent/agent_skills.py`
- Modify: `tests/test_agent_skills.py`
- Create: `tests/test_skill_lifecycle.py`

**Interfaces:**
- Consumes: Task 1 manifest/render/validation interfaces.
- Produces: `AgentAdapter`, `AgentDetection`, `LinkReceipt`, `SkillExportReceipt`, `SkillRemovalReceipt`; `detect_agents(*, home: Path, which: Callable[[str], str | None]) -> tuple[AgentDetection, ...]`; `install_snapshot(files: Mapping[Path, bytes], target: Path, *, force: bool = False) -> SkillExportReceipt`; `export_skill(..., config_root: Path, home: Path, repository: Path | None, link: bool, force: bool, which: Callable) -> SkillExportReceipt`; `unlink_skill(path: Path, *, home: Path, force: bool = False) -> SkillRemovalReceipt`; `remove_skill(path: Path, *, home: Path, force: bool = False) -> SkillRemovalReceipt`.

- [ ] **Step 1: Write failing detection/link-planning tests**

Assert fixed `codex`, `claude`, `opencode` order independent of `which` response order; Codex/OpenCode deduplicate `.agents/skills/<name>`; repository links are relative; correct links are unchanged; wrong links/files/directories are conflicts; escaping links and symlinked parents are rejected; and no conflicting target changes without force.

- [ ] **Step 2: Run detection/link tests and verify RED**

Run: `uv run pytest tests/test_skill_lifecycle.py -k 'detect or link' -v`

Expected: missing lifecycle interfaces.

- [ ] **Step 3: Implement fixed adapters and safe link planning**

Use a constant ordered adapter tuple. Resolve and confine user parents to `home`; resolve repository parents to the Git worktree; deduplicate destinations before mutation; and compare symlink targets without following unexpected links. Create links only after the snapshot is installed and return sorted receipts.

- [ ] **Step 4: Run link tests and verify GREEN**

Run: `uv run pytest tests/test_skill_lifecycle.py -k 'detect or link' -v`

Expected: all selected tests pass.

- [ ] **Step 5: Write failing snapshot/export tests**

Cover new install, byte-identical unchanged install without inode replacement, managed update, modified/unmanaged refusal, forced exact-target replacement, injected failure before `os.replace` preserving the previous snapshot, preferred repository path, actual `git check-ignore` fallback to `.geas/skills`, both paths ignored failure, non-Git repository rejection, and repository export leaving uncommitted files.

- [ ] **Step 6: Run snapshot/export tests and verify RED**

Run: `uv run pytest tests/test_skill_lifecycle.py -k 'install or export or ignored or atomic' -v`

Expected: missing export/install behavior.

- [ ] **Step 7: Implement managed atomic snapshots and repository placement**

Compute and validate the complete candidate in a sibling temporary directory. If a target exists, validate its current manifest/inventory before replacement unless exact `force`; use `git -C <repo> rev-parse --show-toplevel` and `git check-ignore -q -- <path>`; never alter Git metadata; replace with backup/restore semantics; and make identical state a no-write success.

- [ ] **Step 8: Write failing unlink/removal tests**

Assert unlink removes only exact managed symlinks, preserves the snapshot, refuses modified/unmanaged state unless forced, and preserves unrelated files. Assert remove also deletes only the exact validated snapshot, leaves parent directories, reports a deterministic regeneration command, and causes Git to report repository deletions without creating a commit.

- [ ] **Step 9: Implement unlink and removal, run Task 2 tests, and commit**

Run: `uv run pytest tests/test_agent_skills.py tests/test_skill_lifecycle.py -v && uv run ruff check src/research_agent/agent_skills.py tests/test_agent_skills.py tests/test_skill_lifecycle.py`

Commit: `feat: manage portable skill installations`

---

### Task 3: Generic packaged Geas skill and automatic config initialization

**Files:**
- Create: `src/research_agent/builtin_skills/geas/SKILL.md`
- Create: `src/research_agent/builtin_skills/geas/references/cli.md`
- Create: `src/research_agent/builtin_skills/geas/references/security.md`
- Create: `src/research_agent/builtin_skills/geas/references/skills.md`
- Modify: `src/research_agent/agent_skills.py`
- Modify: `src/research_agent/user_config.py`
- Modify: `src/research_agent/cli.py`
- Modify: `pyproject.toml`
- Create: `tests/test_builtin_geas_skill.py`
- Modify: `tests/test_user_config.py`

**Interfaces:**
- Consumes: Task 2 atomic install and agent link planning.
- Produces: `install_builtin_geas_skill(*, config_root: Path, home: Path, which: Callable) -> BuiltinSkillReceipt`; `config-init` JSON key `skills` with sorted `installed`, `updated`, `unchanged`, `linked`, `skipped`, and `conflicts` paths.

- [ ] **Step 1: Establish RED agent-skill scenarios before authoring files**

Before any generic skill file exists, the controller dispatches fresh scenario agents without the skill for: locating an exact citation with provenance, inspecting dissent/gaps/threats, updating/removing an ontology skill, and refusing source-text instructions. Save their verbatim choices and rationalizations in this plan's SDD workspace. In `tests/test_builtin_geas_skill.py`, encode the resulting required routing contracts and keep the no-skill baseline record separate from skill-present assertions.

- [ ] **Step 2: Run baseline tests and verify RED for the absent packaged skill**

Run: `uv run pytest tests/test_builtin_geas_skill.py -v`

Expected: packaged skill/reference paths do not exist and application assertions fail.

- [ ] **Step 3: Author the minimal generic skill and references**

Use only `name` and `description` frontmatter. Keep `SKILL.md` concise and make `references/cli.md`, `references/security.md`, and `references/skills.md` the progressive-disclosure layer. Cover configuration/profiles; ontology/artifact sync; source-library exact search; build/projection/topic/provenance/dissent/gap/threat/temporal/anchor/citation queries; skill export/update/link/unlink/remove; `--help`; source-of-truth boundaries; project URL and optional installation documentation. State that `config-init` reports automatic skill installation and how to remove it.

- [ ] **Step 4: Run skill-present application tests and refine wording**

The controller dispatches fresh scenario agents with the packaged skill available, compares their choices with the RED reports, and the implementer closes observed reference-routing or security loopholes. Then run: `uv run pytest tests/test_builtin_geas_skill.py -v`

Expected: all scenarios can route to the right one-hop reference and command while preserving the security contract.

- [ ] **Step 5: Write failing config-init lifecycle tests**

Run `geas --geas-config <tmp>/config.yaml config-init` with a controlled `HOME` and `PATH`. Assert first-run `installed`/`linked`, second-run `unchanged`, managed packaged-byte change `updated`, manual conflict `conflicts` without overwrite, removal followed by reinstall, Codex/OpenCode deduplication, Claude link creation, valid JSON stdout, and no diagnostics/source text in the receipt.

- [ ] **Step 6: Install package data and integrate config-init**

Use `importlib.resources.files("research_agent").joinpath("builtin_skills/geas")`, copy only regular non-symlink package files through the atomic installer, and include recursive package data in the wheel. `UserConfigManager.load_or_create` or the CLI orchestration invokes installation explicitly, and the CLI reports what it is doing on stderr plus the structured receipt on stdout. Preserve unmanaged conflicts.

- [ ] **Step 7: Run Task 3 tests, inspect the built wheel, and commit**

Run: `uv build && unzip -l dist/*.whl | rg 'builtin_skills/geas' && uv run pytest tests/test_builtin_geas_skill.py tests/test_user_config.py -v && uv run ruff check src/research_agent/agent_skills.py src/research_agent/user_config.py src/research_agent/cli.py tests/test_builtin_geas_skill.py tests/test_user_config.py`

Commit: `feat: install the generic Geas agent skill`

---

### Task 4: Trusted update provenance and CLI lifecycle orchestration

**Files:**
- Create: `src/research_agent/geas_update.py`
- Create: `tests/test_geas_update.py`
- Modify: `src/research_agent/agent_skills.py`
- Modify: `src/research_agent/cli.py`
- Create: `tests/test_skill_cli.py`

**Interfaces:**
- Consumes: Task 1 render/manifest, Task 2 lifecycle, existing `OntologyRepositoryManager` and artifact manager, trusted selected `GeasProfile`.
- Produces: `GeasInstallProvenance`, `GeasUpdateReceipt`, `GeasUpdater.inspect()`, `GeasUpdater.update_and_reexec(argv: Sequence[str], *, continuation: str | None) -> GeasUpdateReceipt | NoReturn`; CLI commands `skill-export`, `skill-update`, `skill-unlink`, and `skill-remove` with `--force` where the spec authorizes exact-target override.

- [ ] **Step 1: Write failing provenance/updater tests**

Use injected command runners and temporary Git repositories/uv receipt TOML. Cover a clean directory-backed uv tool, Git development invocation, unknown installer, malformed/ambiguous receipt, dirty checkout, trusted remote mismatch, diverged branch, fetch failure, ff-only success, reinstall failure, post-reexec version mismatch, and repeated continuation marker. Assert rejected cases make no install/reexec call.

- [ ] **Step 2: Run updater tests and verify RED**

Run: `uv run pytest tests/test_geas_update.py -v`

Expected: `research_agent.geas_update` is absent.

- [ ] **Step 3: Implement trusted explicit self-update**

Trust only the fixed Geas project URL `https://github.com/Epiphytic/geas.git` (normalize its `.git` spelling) and branch `main`. Parse the uv receipt strictly enough to identify a single directory requirement; verify clean Git and remote; fetch; require `merge-base --is-ancestor HEAD origin/main`; update with `merge --ff-only`; reinstall the exact directory with `uv tool install --force <directory>`; and use an environment continuation token containing old/new commit and one-hop depth. Never derive commands or URLs from the skill manifest.

- [ ] **Step 4: Run updater tests and verify GREEN**

Run: `uv run pytest tests/test_geas_update.py -v`

Expected: all updater tests pass offline.

- [ ] **Step 5: Write failing CLI import/export/setup/install/uninstall/update tests**

Build a temporary ontology Git remote/profile and a deterministic test projection. Exercise exact CLI parsing/JSON for user and repository export, repeat-export unchanged, profile URL/branch mismatch, ontology fast-forward update, artifact verification failure preserving the old snapshot, bounded Geas reexec, unlink, remove, force rejection/acceptance, and invalid manifest/path inputs. Assert stdout is one JSON document, stderr holds progress, and external command arguments exactly match the trusted plan.

- [ ] **Step 6: Implement CLI orchestration and update phases**

Add parser flags exactly:

```text
geas skill-export ONTOLOGY [--name NAME] [--link] [--repo PATH] [--force]
geas skill-update SKILL_PATH [--force] [--geas-update-continuation TOKEN]
geas skill-unlink SKILL_PATH [--force]
geas skill-remove SKILL_PATH [--force]
```

Resolve the ontology only from the active trusted profile/catalog, require manifest URL/branch equality on update, use fast-forward-only ontology sync and verified artifacts, select/build its portable knowledge projection, render all candidate bytes before replacement, repair managed links, and emit sorted phase receipts. If the existing projection cannot identify a unique export topic, fail with a command showing the explicit ontology configuration needed instead of guessing.

- [ ] **Step 7: Run Task 4 tests and commit**

Run: `uv run pytest tests/test_geas_update.py tests/test_skill_cli.py tests/test_ontology_sync.py tests/test_ontology_artifacts.py -v && uv run ruff check src/research_agent/geas_update.py src/research_agent/agent_skills.py src/research_agent/cli.py tests/test_geas_update.py tests/test_skill_cli.py`

Commit: `feat: add trusted skill lifecycle commands`

---

### Task 5: End-to-end determinism, docs, maintained demo, and GitHub Actions

**Files:**
- Create: `.github/workflows/ci.yml`
- Modify: `ontology/open-source-research-agents/demo.sh`
- Modify: `README.md`
- Modify: `docs/GETTING_STARTED.md`
- Modify: `docs/QUICKSTART_ONTOLOGY.md`
- Modify: `docs/USER_CONFIG.md`
- Create: `docs/AGENT_SKILLS.md`
- Create: `tests/test_skill_end_to_end.py`
- Modify: tests from Tasks 1–4 as required by end-to-end discoveries.

**Interfaces:**
- Consumes: all prior public CLI commands and receipts.
- Produces: documented operator workflow, maintained demo export, and CI jobs for lint/unit/lifecycle/demo determinism.

- [ ] **Step 1: Write failing end-to-end lifecycle tests**

In one temporary user config and one temporary Git worktree, test setup → generic install → ontology import/profile sync → export → identical export → trusted update → unlink → relink/export → remove → reinstall. Run repository export twice from the same exact Git/projection identities and compare every portable byte/digest. Verify snapshots work with `geas` removed from `PATH`, preserve external links and provenance, omit full acquired-document sentinels, and leave repository additions/deletions reviewable.

- [ ] **Step 2: Run end-to-end tests and verify RED**

Run: `uv run pytest tests/test_skill_end_to_end.py -v`

Expected: failures identify any missing cross-component lifecycle behavior.

- [ ] **Step 3: Close end-to-end gaps test-first**

For each failure, keep the new failing assertion, make the minimum implementation adjustment in the owning module, and rerun the focused test until green. Do not weaken profile/remote/path/inventory checks to satisfy fixtures.

- [ ] **Step 4: Extend the maintained demo**

After its deterministic projection build, export the example ontology skill into the demo root twice, compare its digest and file hashes, validate its manifest, and write the receipt artifacts under the supplied ignored demo root.

- [ ] **Step 5: Add operator documentation**

Document exact command workflows, standard agent paths, link deduplication, repository fallback behavior, optional Geas installation URL, explicit automatic update boundary, trusted provenance limitations, unlink versus remove, deterministic receipts, and that skill sources are references/evidence excerpts rather than bundled full documents.

- [ ] **Step 6: Add GitHub Actions checks**

Create an offline Python 3.12 workflow with least-privilege `contents: read`, uv caching, `uv sync --extra dev`, Ruff, the full pytest suite, a named lifecycle test step, `git diff --check`, wheel package-data inspection, and the maintained demo using a temporary directory. Do not add secrets or network-backed model/source calls.

- [ ] **Step 7: Run focused Task 5 checks and commit**

Run: `uv run pytest tests/test_skill_end_to_end.py tests/test_builtin_geas_skill.py tests/test_skill_cli.py -v && demo_root=$(mktemp -d /tmp/geas-skill-demo.XXXXXX) && ./ontology/open-source-research-agents/demo.sh "$demo_root" && git diff --check`

Commit: `test: verify deterministic skill lifecycle in CI`

---

### Task 6: Whole-branch verification and review

**Files:**
- Modify only files required to address verified review findings.

**Interfaces:**
- Consumes: completed Tasks 1–5 and the approved spec.
- Produces: a review-ready pushed feature branch.

- [ ] **Step 1: Verify dependency and package state**

Run: `uv sync --extra dev && uv build`

Expected: exit 0 and the built wheel contains `builtin_skills/geas`.

- [ ] **Step 2: Run all static and test checks**

Run: `uv run ruff check . && uv run pytest -q && git diff --check`

Expected: exit 0, zero failures, and only documented skips.

- [ ] **Step 3: Run command/demo smoke checks**

Run: `uv run geas --help && demo_root=$(mktemp -d /tmp/geas-final-demo.XXXXXX) && ./ontology/open-source-research-agents/demo.sh "$demo_root"`

Expected: the CLI lists all four skill commands and the demo validates deterministic export.

- [ ] **Step 4: Dispatch final whole-branch code review**

Review the complete branch against the design spec, with special attention to authority flow, exact-target deletion, symlink/path traversal, trusted updater provenance, deterministic bytes, and CI offline behavior. Fix all Critical/Important findings via a subagent and re-review the fix diff once.

- [ ] **Step 5: Push the review branch**

Push `feature/geas-agent-skill-lifecycle` to `origin` without merging to `main`, then report the branch/commit, checks, review findings, and any explicit rulings.
