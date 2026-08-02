# Deterministic model-use authorization

## Boundary

Local DeepSeek is the default model route. External providers cannot be reached
through `ModelClient` unless a deterministic `ModelUseGate` authorizes the
exact call before network I/O.

The authorization binds:

- configured provider name, exact model, and exact base URL;
- declared operation and data classification;
- metadata-only or source-content input;
- the deposit's local-preferred or external-allowed routing label;
- SHA-256 of the exact system and user inputs;
- maximum output tokens;
- policy version and human-approval state.

Successful authorizations are immutable records. They do not contain prompts or
credentials.

## Current external policy

`config/model-policy.yaml` allowlists OpenAI and z.ai endpoints, models,
operations, and data classes. This allowlist is necessary but not currently
sufficient for an automatic external call.

Automatic external use is schema-locked off until the operator chooses cost
thresholds and the system has a persistent usage ledger. Until then, external
calls require `--approve-external-provider`.

Additional fail-closed rules apply:

- unknown data classifications cannot be sent externally, even with the
  approval flag;
- source content must be marked `external_allowed`;
- a provider name cannot substitute a different model or base URL;
- external endpoints require HTTPS;
- providers marked local must use a literal loopback address;
- model HTTP redirects are rejected;
- credentials, destinations, operations, and authorization fields are never
  accepted from model output or source content.

## CLI examples

Local query compilation remains automatic:

```bash
uv run research-agent research-local "Map this topic" \
  --corpus corpus \
  --compiler-provider deepseek_local
```

An external compiler requires both a trusted classification and explicit
approval while automatic calls are disabled:

```bash
uv run research-agent research-local "Map this topic" \
  --corpus corpus \
  --compiler-provider openai \
  --compiler-data-class authorized_workspace \
  --approve-external-provider
```

The model compiler receives the research question, controlled vocabulary, and
connector manifests. It does not receive acquired source content in this
workflow.

## Security meaning

Authorization is a deterministic policy result, not a model judgment. A model
may recommend escalation, but it cannot change the provider, classification,
route, operation, endpoint, or approval state used by the gate.

The current CLI approval flag records that approval occurred; it is not proof
of an authenticated human identity. A later operator decision may select a
signed or authenticated approval mechanism.
