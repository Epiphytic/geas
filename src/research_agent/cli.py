from __future__ import annotations

import argparse
import json
from pathlib import Path

from research_agent.connectors import LocalFileConnector
from research_agent.discovery import CompilerIdentity
from research_agent.models import (
    PolicyStage,
    ThreatObservation,
    ThreatTarget,
)
from research_agent.planning import (
    ConceptVocabulary,
    ModelQueryCompiler,
    QueryPlanValidator,
    QueryProposal,
    deterministic_proposal,
)
from research_agent.policy import PolicyEngine
from research_agent.providers import ModelClient, load_provider_configs
from research_agent.research import OfflineResearchRunner
from research_agent.store import ImmutableStore
from research_agent.workflow import ActorKind, WorkflowEngine, WorkflowState


def _json(value: object) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    print(json.dumps(value, indent=2, sort_keys=True))


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
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("providers", help="list configured providers without secrets")

    smoke = subparsers.add_parser("model-smoke", help="run a tool-free model smoke test")
    smoke.add_argument("--provider")

    init = subparsers.add_parser("store-init", help="initialize an immutable store")
    init.add_argument("--root", type=Path, default=Path("data"))

    source = subparsers.add_parser("source-add", help="archive a local source file")
    source.add_argument("path", type=Path)
    source.add_argument("--root", type=Path, default=Path("data"))
    source.add_argument("--uri")
    source.add_argument("--license")

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
    offline.add_argument("--topic-branch", default="topic:local")

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
        client = ModelClient(name, providers[name])
        result = client.complete_json(
            system=(
                "Return one JSON object only. Do not call tools. "
                'The schema is {"status":"ok","capabilities":["string"]}.'
            ),
            user="Report that the tool-free research extraction model endpoint is available.",
            max_output_tokens=256,
        )
        _json({"provider": name, "result": result})
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
            proposal = ModelQueryCompiler(ModelClient(args.compiler_provider, provider)).compile(
                args.question,
                vocabulary=vocabulary,
                manifests={connector.manifest.id: connector.manifest},
            )
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
