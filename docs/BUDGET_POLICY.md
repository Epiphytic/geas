# Usage budgets and accounting treatment

## Conservative automatic envelope

External model calls may run automatically only after a transactional
reservation succeeds in `data/usage.sqlite`. The checked-in limits are:

- US$0.25 reserved cost per call;
- 32,000 reserved input tokens and 8,192 output tokens per call;
- 10 automatic external calls and US$2 per run;
- US$5 per UTC day;
- US$25 per UTC month.

Dollar values are stored as integer millionths of a US dollar. The ledger uses
`BEGIN IMMEDIATE`, so concurrent agents cannot each spend the same remaining
allowance.

Before a request, the system reserves a conservative input-token bound and the
maximum possible output. After a response, it settles against provider-reported
usage. Missing usage charges the full reservation. Usage beyond the reservation
is recorded as an overrun and the model output is rejected.

## Account treatment

Each provider account has two separate fields:

- `billing_basis`: `metered`, `subscription_included`, `enterprise_commit`,
  `no_marginal_cost`, or `other`;
- `budget_treatment`: `counted` or `excluded_from_cost`.

An exclusion requires a non-metered billing basis and a nonempty accounting
note. It removes that account's calls from dollar totals only. Automatic calls
still consume per-run call counts and per-call token limits, remain subject to
provider/model/data policy, and create authorization and usage records.

For example, an operator-confirmed subscription can be represented as:

```yaml
billing_basis: subscription_included
budget_treatment: excluded_from_cost
accounting_note: "Included in organization plan ABC through 2027-01-01"
input_cost_microusd_per_million_tokens: null
output_cost_microusd_per_million_tokens: null
```

The checked-in OpenAI and Z.ai accounts remain `metered` and `counted`; the
repository does not assume that a consumer, coding, or enterprise subscription
includes API usage.

## Search and other services

The accounting vocabulary is service-neutral and includes model, search, and
other usage. A search account may likewise be classified as metered,
subscription-included, or covered by an enterprise commitment.

The initial transactional adapter is connected to model calls. Mojeek retains
its existing hard per-run request cap, while its monthly cost enforcement and
generic search-ledger adapter remain disabled until the account's tier and unit
accounting are confirmed. Excluding search cost in the future will not remove
its request caps.

## Human overrides

An explicitly approved call may exceed automatic call or dollar limits, but it
cannot exceed hard per-call token limits, use unknown accounting, send
unknown-classification data, or bypass source-content routing rules.

For this CLI-first application, the local OS account is the authenticated
identity. The override flag creates and consumes a single-use receipt bound to
the exact request. See `docs/APPROVALS.md`.
