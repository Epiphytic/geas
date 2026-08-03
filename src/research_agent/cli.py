from __future__ import annotations

import argparse
import getpass
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic_core import to_jsonable_python

from research_agent.approvals import ApprovalRegistry, AuthenticatedPrincipal
from research_agent.budget import BudgetPolicy, UsageLedger
from research_agent.connectors import LocalFileConnector, MojeekDiscoveryConnector
from research_agent.deposits import (
    AcquisitionMethod,
    DepositManager,
    DepositOverrides,
    DepositPolicy,
    ModelRoute,
    NostrClaim,
    NostrEvent,
    PermissionStatus,
    RedistributionStatus,
    UsagePermissionOverrides,
)
from research_agent.discovery import (
    CompilerIdentity,
    ConnectorCapability,
    SourceClass,
)
from research_agent.model_policy import (
    DataClass,
    InputKind,
    ModelOperation,
    ModelUseContext,
    ModelUseGate,
    ModelUsePolicy,
)
from research_agent.models import (
    PolicyStage,
    ThreatObservation,
    ThreatTarget,
)
from research_agent.operator_policy import ResearchPolicy
from research_agent.planning import (
    ConceptVocabulary,
    ModelQueryCompiler,
    QueryPlanValidator,
    QueryProposal,
    deterministic_proposal,
)
from research_agent.policy import PolicyEngine
from research_agent.providers import ModelClient, load_provider_configs
from research_agent.research import DiscoveryExecutor, OfflineResearchRunner
from research_agent.secrets import load_env_file
from research_agent.store import ImmutableStore
from research_agent.truth import SQLiteProjectionGuard, TruthManager, TruthPolicy, TruthSnapshot
from research_agent.workflow import ActorKind, WorkflowEngine, WorkflowState


def _json(value: object) -> None:
    value = to_jsonable_python(value)
    print(json.dumps(value, indent=2, sort_keys=True))


def _local_approval_principal(root: Path) -> AuthenticatedPrincipal:
    uid = getattr(os, "getuid", lambda: -1)()
    return AuthenticatedPrincipal(
        actor_id=f"os-user:{uid}:{getpass.getuser()}",
        deployment_id=f"local:{root.resolve()}",
        session_id=f"process:{os.getpid()}",
        authenticated_at=datetime.now(UTC),
        authentication_method="local_os_session",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="research-agent")
    parser.add_argument(
        "--providers",
        type=Path,
        default=Path("config/providers.toml"),
        help="provider configuration path",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path("config/source-policy.yaml"),
        help="deterministic source policy path",
    )
    parser.add_argument(
        "--research-policy",
        type=Path,
        default=Path("config/research-policy.yaml"),
        help="connector priority, storage, and cost policy path",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="ignored secret environment file",
    )
    parser.add_argument(
        "--truth-policy",
        type=Path,
        default=Path("config/truth-policy.yaml"),
        help="canonical-source and projection reconciliation policy",
    )
    parser.add_argument(
        "--deposit-policy",
        type=Path,
        default=Path("config/deposit-policy.yaml"),
        help="user-deposit defaults and authorization-boundary policy",
    )
    parser.add_argument(
        "--model-policy",
        type=Path,
        default=Path("config/model-policy.yaml"),
        help="deterministic local and external model-use policy",
    )
    parser.add_argument(
        "--budget-policy",
        type=Path,
        default=Path("config/budget-policy.yaml"),
        help="automatic external-use envelope and accounting treatment",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("providers", help="list configured providers without secrets")

    smoke = subparsers.add_parser("model-smoke", help="run a tool-free model smoke test")
    smoke.add_argument("--provider")
    smoke.add_argument("--root", type=Path, default=Path("data"))
    smoke.add_argument("--run-id")
    smoke.add_argument("--approval-receipt-id")
    smoke.add_argument("--override-external-budget", action="store_true")

    init = subparsers.add_parser("store-init", help="initialize an immutable store")
    init.add_argument("--root", type=Path, default=Path("data"))

    source = subparsers.add_parser("source-add", help="archive a local source file")
    source.add_argument("path", type=Path)
    source.add_argument("--root", type=Path, default=Path("data"))
    source.add_argument("--uri")
    source.add_argument("--license")

    deposit = subparsers.add_parser(
        "deposit-add",
        help="archive a file with provenance and user-controlled handling defaults",
    )
    deposit.add_argument("path", type=Path)
    deposit.add_argument("--root", type=Path, default=Path("data"))
    deposit.add_argument("--deposited-by", required=True)
    deposit.add_argument(
        "--method",
        type=AcquisitionMethod,
        choices=list(AcquisitionMethod),
        default=AcquisitionMethod.LOCAL_FILE,
    )
    deposit.add_argument("--original-locator")
    deposit.add_argument("--source-uri")
    deposit.add_argument("--license")
    deposit.add_argument("--author", action="append", default=[])
    deposit.add_argument("--usage-condition", action="append", default=[])
    deposit.add_argument("--rights-basis")
    deposit.add_argument("--provenance-note")
    deposit.add_argument("--nostr-ownership-event", type=Path, action="append", default=[])
    deposit.add_argument("--nostr-authorship-event", type=Path, action="append", default=[])
    deposit.add_argument("--nostr-publication-event", type=Path, action="append", default=[])
    deposit.add_argument("--scope-label")
    deposit.add_argument(
        "--index-content",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    deposit.add_argument(
        "--include-in-ontology",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    deposit.add_argument("--model-route", type=ModelRoute, choices=list(ModelRoute))
    deposit.add_argument(
        "--redistribution-status",
        type=RedistributionStatus,
        choices=list(RedistributionStatus),
    )
    deposit.add_argument(
        "--archive-permission", type=PermissionStatus, choices=list(PermissionStatus)
    )
    deposit.add_argument(
        "--quote-permission", type=PermissionStatus, choices=list(PermissionStatus)
    )
    deposit.add_argument(
        "--transform-permission", type=PermissionStatus, choices=list(PermissionStatus)
    )
    deposit.add_argument(
        "--redistribute-original-permission",
        type=PermissionStatus,
        choices=list(PermissionStatus),
    )
    deposit.add_argument("--retention-policy")

    offline = subparsers.add_parser(
        "research-local",
        help="run deterministic discovery and acquisition over local roots",
    )
    offline.add_argument("question")
    offline.add_argument("--root", type=Path, default=Path("data"))
    offline.add_argument("--corpus", type=Path, action="append", required=True)
    offline.add_argument("--term", action="append", default=[])
    offline.add_argument("--concept", action="append", default=[])
    offline.add_argument(
        "--vocabulary",
        type=Path,
        default=Path("config/query-vocabulary.yaml"),
    )
    offline.add_argument("--result-limit", type=int, default=50)
    offline.add_argument("--approve-budget", action="store_true")
    offline.add_argument(
        "--compiler-provider",
        help="tool-free configured model provider; omit for deterministic lexical compilation",
    )
    offline.add_argument(
        "--compiler-data-class",
        type=DataClass,
        choices=list(DataClass),
        default=DataClass.UNKNOWN,
        help="trusted classification of compiler input; unknown forbids external use",
    )
    offline.add_argument("--approval-receipt-id")
    offline.add_argument("--override-external-budget", action="store_true")
    offline.add_argument("--run-id")
    offline.add_argument("--topic-branch", default="topic:local")

    mojeek = subparsers.add_parser(
        "discover-mojeek",
        help="run discovery-only search; results are not evidence",
    )
    mojeek.add_argument("question")
    mojeek.add_argument("--root", type=Path, default=Path("data"))
    mojeek.add_argument("--term", action="append", default=[])
    mojeek.add_argument("--concept", action="append", default=[])
    mojeek.add_argument(
        "--vocabulary",
        type=Path,
        default=Path("config/query-vocabulary.yaml"),
    )
    mojeek.add_argument("--result-limit", type=int, default=10)
    mojeek.add_argument("--approve-budget", action="store_true")

    truth_snapshot = subparsers.add_parser(
        "truth-snapshot",
        help="capture the canonical ontology, schemas, records, and blobs",
    )
    truth_snapshot.add_argument("--root", type=Path, default=Path("data"))
    truth_snapshot.add_argument("--workspace", type=Path, default=Path("."))
    truth_snapshot.add_argument("--created-by", required=True)
    truth_snapshot.add_argument("--predecessor")

    truth_check = subparsers.add_parser(
        "truth-check",
        help="detect drift from a canonical truth snapshot",
    )
    truth_check.add_argument("snapshot", type=Path)
    truth_check.add_argument("--root", type=Path, default=Path("data"))
    truth_check.add_argument("--workspace", type=Path, default=Path("."))

    projection_stamp = subparsers.add_parser(
        "projection-stamp",
        help="stamp a completely built SQLite projection",
    )
    projection_stamp.add_argument("snapshot", type=Path)
    projection_stamp.add_argument("database", type=Path)
    projection_stamp.add_argument("--schema-version", type=int, required=True)
    projection_stamp.add_argument("--builder-version", required=True)

    projection_check = subparsers.add_parser(
        "projection-check",
        help="detect canonical or SQLite projection drift",
    )
    projection_check.add_argument("snapshot", type=Path)
    projection_check.add_argument("database", type=Path)
    projection_check.add_argument("--root", type=Path, default=Path("data"))
    projection_check.add_argument("--workspace", type=Path, default=Path("."))

    policy = subparsers.add_parser("policy-check", help="evaluate source policy")
    policy.add_argument("--workflow-id", required=True)
    policy.add_argument("--source-version", required=True)
    policy.add_argument("--stage", type=PolicyStage, choices=list(PolicyStage), required=True)
    policy.add_argument("observations", nargs="*", type=Path)

    transition = subparsers.add_parser("workflow-transition", help="validate a state transition")
    transition.add_argument("--workflow-id", required=True)
    transition.add_argument("--source-version", required=True)
    transition.add_argument(
        "--from-state",
        type=WorkflowState,
        choices=list(WorkflowState),
        required=True,
    )
    transition.add_argument(
        "--to-state",
        type=WorkflowState,
        choices=list(WorkflowState),
        required=True,
    )
    transition.add_argument("--actor-kind", type=ActorKind, choices=list(ActorKind), required=True)
    transition.add_argument("--actor-id", required=True)
    transition.add_argument("--artifact-hash", action="append", default=[])
    return parser


def main() -> None:
    args = _build_parser().parse_args()

    if args.command == "providers":
        default, providers = load_provider_configs(args.providers)
        _json(
            {
                "default": default,
                "providers": {
                    name: {
                        "base_url": str(config.base_url),
                        "model": config.model,
                        "external": config.external,
                        "api_key_env": config.api_key_env or None,
                    }
                    for name, config in providers.items()
                },
            }
        )
        return

    if args.command == "model-smoke":
        default, providers = load_provider_configs(args.providers)
        name = args.provider or default
        gate = ModelUseGate(
            ModelUsePolicy.from_yaml(args.model_policy),
            ModelUseContext(
                operation=ModelOperation.MODEL_SMOKE,
                data_class=DataClass.PUBLIC,
                input_kind=InputKind.METADATA_ONLY,
                approval_receipt_id=args.approval_receipt_id,
                run_id=args.run_id or f"run:model-smoke:{uuid4()}",
            ),
            budget_policy=BudgetPolicy.from_yaml(args.budget_policy),
            usage_ledger=UsageLedger(args.root / "usage.sqlite"),
            approval_registry=ApprovalRegistry(args.root / "usage.sqlite"),
            override_principal=(
                _local_approval_principal(args.root) if args.override_external_budget else None
            ),
        )
        client = ModelClient(
            name,
            providers[name],
            gate=gate,
        )
        result = client.complete_json(
            system=(
                "Return one JSON object only. Do not call tools. "
                'The schema is {"status":"ok","capabilities":["string"]}.'
            ),
            user="Report that the tool-free research extraction model endpoint is available.",
            max_output_tokens=256,
        )
        store = ImmutableStore(args.root)
        store.initialize()
        authorization_hash = store.put_record(
            "model-authorization",
            gate.last_authorization,
        )
        settlement_hash = (
            store.put_record("usage-settlement", gate.last_settlement)
            if gate.last_settlement is not None
            else None
        )
        approval_hash = (
            store.put_record("approval-receipt", gate.last_approval_receipt)
            if gate.last_approval_receipt is not None
            else None
        )
        _json(
            {
                "provider": name,
                "result": result,
                "authorization": gate.last_authorization,
                "authorization_record_hash": authorization_hash,
                "usage_settlement": gate.last_settlement,
                "usage_settlement_record_hash": settlement_hash,
                "approval_receipt": gate.last_approval_receipt,
                "approval_receipt_record_hash": approval_hash,
            }
        )
        return

    if args.command == "store-init":
        store = ImmutableStore(args.root)
        store.initialize()
        _json({"root": str(store.root), "initialized": True})
        return

    if args.command == "source-add":
        store = ImmutableStore(args.root)
        store.initialize()
        source = store.ingest_file(
            args.path,
            source_uri=args.uri,
            license=args.license,
        )
        _json(source)
        return

    if args.command == "deposit-add":
        store = ImmutableStore(args.root)
        store.initialize()
        nostr_evidence = tuple(
            (NostrEvent.model_validate_json(path.read_bytes()), claim)
            for paths, claim in (
                (args.nostr_ownership_event, NostrClaim.OWNERSHIP),
                (args.nostr_authorship_event, NostrClaim.AUTHORSHIP),
                (args.nostr_publication_event, NostrClaim.PUBLICATION),
            )
            for path in paths
        )
        permission_values = {
            "archive": args.archive_permission,
            "quote": args.quote_permission,
            "transform": args.transform_permission,
            "redistribute_original": args.redistribute_original_permission,
        }
        result = DepositManager(
            store=store,
            policy=DepositPolicy.from_yaml(args.deposit_policy),
        ).deposit_file(
            args.path,
            deposited_by=args.deposited_by,
            acquisition_method=args.method,
            original_locator=args.original_locator,
            source_uri=args.source_uri,
            license=args.license,
            authors=tuple(args.author),
            usage_conditions=tuple(args.usage_condition),
            rights_basis=args.rights_basis,
            provenance_note=args.provenance_note,
            nostr_evidence=nostr_evidence,
            overrides=DepositOverrides(
                scope_label=args.scope_label,
                index_content=args.index_content,
                include_in_ontology=args.include_in_ontology,
                model_route=args.model_route,
                redistribution_status=args.redistribution_status,
                usage_permissions=(
                    UsagePermissionOverrides.model_validate(permission_values)
                    if any(value is not None for value in permission_values.values())
                    else None
                ),
                retention_policy=args.retention_policy,
            ),
        )
        _json(result)
        return

    if args.command == "research-local":
        store = ImmutableStore(args.root)
        store.initialize()
        connector = LocalFileConnector(args.corpus)
        vocabulary = ConceptVocabulary.from_yaml(args.vocabulary)
        if args.compiler_provider:
            _, providers = load_provider_configs(args.providers)
            if args.compiler_provider not in providers:
                raise ValueError(f"unknown provider: {args.compiler_provider}")
            provider = providers[args.compiler_provider]
            gate = ModelUseGate(
                ModelUsePolicy.from_yaml(args.model_policy),
                ModelUseContext(
                    operation=ModelOperation.QUERY_COMPILATION,
                    data_class=args.compiler_data_class,
                    input_kind=InputKind.METADATA_ONLY,
                    approval_receipt_id=args.approval_receipt_id,
                    run_id=args.run_id or f"run:research-local:{uuid4()}",
                ),
                budget_policy=BudgetPolicy.from_yaml(args.budget_policy),
                usage_ledger=UsageLedger(args.root / "usage.sqlite"),
                approval_registry=ApprovalRegistry(args.root / "usage.sqlite"),
                override_principal=(
                    _local_approval_principal(args.root) if args.override_external_budget else None
                ),
            )
            client = ModelClient(
                args.compiler_provider,
                provider,
                gate=gate,
            )
            proposal = ModelQueryCompiler(client).compile(
                args.question,
                vocabulary=vocabulary,
                manifests={connector.manifest.id: connector.manifest},
            )
            store.put_record("model-authorization", gate.last_authorization)
            if gate.last_settlement is not None:
                store.put_record("usage-settlement", gate.last_settlement)
            if gate.last_approval_receipt is not None:
                store.put_record("approval-receipt", gate.last_approval_receipt)
            compiler = CompilerIdentity(
                id=f"compiler:model:{args.compiler_provider}:{provider.model}",
                version=ModelQueryCompiler.version,
            )
        else:
            proposal = deterministic_proposal(
                args.question,
                connector_id=connector.manifest.id,
                concept_ids=tuple(args.concept),
            )
            compiler = CompilerIdentity(id="compiler:deterministic-lexical", version="1")
        if args.concept:
            proposal = proposal.model_copy(
                update={"concept_ids": tuple(sorted(set(proposal.concept_ids) | set(args.concept)))}
            )
        if args.term:
            proposal = QueryProposal.model_validate(
                {
                    **proposal.model_dump(mode="json"),
                    "exact_terms": args.term,
                    "result_limit": args.result_limit,
                }
            )
        else:
            proposal = proposal.model_copy(update={"result_limit": args.result_limit})
        plan = QueryPlanValidator(
            vocabulary=vocabulary,
            manifests={connector.manifest.id: connector.manifest},
        ).validate(
            proposal,
            compiler=compiler,
            human_approved=args.approve_budget,
        )
        result = OfflineResearchRunner(store=store, connector=connector).run(
            plan,
            topic_branch=args.topic_branch,
        )
        _json(result)
        return

    if args.command == "discover-mojeek":
        research_policy = ResearchPolicy.from_yaml(args.research_policy)
        provider_policy = research_policy.provider("connector:mojeek")
        if not provider_policy.enabled:
            raise ValueError("Mojeek is disabled by the research policy")
        load_env_file(
            args.env_file,
            allowed_names=frozenset({provider_policy.credential_env}),
        )
        connector = MojeekDiscoveryConnector()
        vocabulary = ConceptVocabulary.from_yaml(args.vocabulary)
        base = deterministic_proposal(
            args.question,
            connector_id=connector.manifest.id,
            concept_ids=tuple(args.concept),
        )
        proposal = QueryProposal.model_validate(
            {
                **base.model_dump(mode="json"),
                "exact_terms": args.term or base.exact_terms,
                "source_classes": [SourceClass.WEB],
                "capabilities": [
                    ConnectorCapability.DISCOVERY,
                    ConnectorCapability.METADATA,
                ],
                "result_limit": args.result_limit,
                "page_limit": min(
                    math.ceil(args.result_limit / 40),
                    provider_policy.max_requests_per_run,
                ),
            }
        )
        plan = QueryPlanValidator(
            vocabulary=vocabulary,
            manifests={connector.manifest.id: connector.manifest},
        ).validate(
            proposal,
            compiler=CompilerIdentity(id="compiler:deterministic-lexical", version="1"),
            human_approved=args.approve_budget,
        )
        execution = DiscoveryExecutor().run(plan, connector)
        store = ImmutableStore(args.root)
        store.initialize()
        record_hashes = {
            "research-policy": (store.put_record("research-policy", research_policy),),
            "query-plan": (store.put_record("query-plan", plan),),
            "connector-manifest": (store.put_record("connector-manifest", connector.manifest),),
            "discovery-run": (store.put_record("discovery-run", execution.discovery_run),),
        }
        if provider_policy.persist_normalized_results:
            record_hashes["discovery-hit"] = tuple(
                store.put_record("discovery-hit", hit) for hit in execution.hits
            )
        _json(
            {
                "query_plan": plan,
                "discovery_run": execution.discovery_run,
                "hits": execution.hits,
                "persistence": {
                    "normalized_results": provider_policy.persist_normalized_results,
                    "storage_rights": provider_policy.storage_rights,
                    "raw_response_retention_days": (provider_policy.raw_response_retention_days),
                    "note": "Search results are discovery metadata, never evidence.",
                },
                "record_hashes": record_hashes,
                "acquisition_priority": research_policy.open_source_acquisition_order,
            }
        )
        return

    if args.command == "truth-snapshot":
        store = ImmutableStore(args.root)
        store.initialize()
        manager = TruthManager(
            workspace_root=args.workspace,
            store_root=store.root,
            policy=TruthPolicy.from_yaml(args.truth_policy),
        )
        snapshot = manager.capture(
            created_by=args.created_by,
            predecessor=args.predecessor,
        )
        digest = store.put_record("truth-snapshot", snapshot)
        _json(
            {
                "snapshot": snapshot,
                "record_digest": digest,
                "record_path": str(store.record_path("truth-snapshot", digest)),
            }
        )
        return

    if args.command == "truth-check":
        snapshot = TruthSnapshot.model_validate_json(args.snapshot.read_text())
        report = TruthManager(
            workspace_root=args.workspace,
            store_root=args.root,
            policy=TruthPolicy.from_yaml(args.truth_policy),
        ).verify(snapshot)
        _json(report)
        if not report.clean:
            raise SystemExit(2)
        return

    if args.command == "projection-stamp":
        snapshot = TruthSnapshot.model_validate_json(args.snapshot.read_text())
        stamp = SQLiteProjectionGuard().stamp(
            args.database,
            snapshot,
            schema_version=args.schema_version,
            builder_version=args.builder_version,
        )
        _json(stamp)
        return

    if args.command == "projection-check":
        snapshot = TruthSnapshot.model_validate_json(args.snapshot.read_text())
        truth_report = TruthManager(
            workspace_root=args.workspace,
            store_root=args.root,
            policy=TruthPolicy.from_yaml(args.truth_policy),
        ).verify(snapshot)
        report = SQLiteProjectionGuard().verify(
            args.database,
            snapshot,
            truth_report=truth_report,
        )
        _json(report)
        if not report.clean:
            raise SystemExit(2)
        return

    if args.command == "policy-check":
        observations = [
            ThreatObservation.model_validate_json(path.read_text()) for path in args.observations
        ]
        target = ThreatTarget(source_version=args.source_version)
        decision = PolicyEngine.from_yaml(args.policy).evaluate(
            target=target,
            workflow_id=args.workflow_id,
            stage=args.stage,
            observations=observations,
        )
        _json(decision)
        return

    if args.command == "workflow-transition":
        event = WorkflowEngine().transition(
            workflow_id=args.workflow_id,
            source_version=args.source_version,
            from_state=args.from_state,
            to_state=args.to_state,
            actor_kind=args.actor_kind,
            actor_id=args.actor_id,
            artifact_hashes=tuple(args.artifact_hash),
        )
        _json(event)
        return

    raise AssertionError(f"unhandled command: {args.command}")
