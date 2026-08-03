# Authenticated approvals

## CLI-first boundary

The initial application is primarily local CLI software. The operating-system
account running the process is therefore the authenticated approval identity.
`--override-external-budget` records the numeric UID, account name, local
deployment root, and process session before issuing an approval.

The flag does not directly set `human_approved=true`. It causes the trusted CLI
adapter to:

1. build an approval request for the exact model call;
2. issue a five-minute receipt attributed to the local OS principal;
3. consume that receipt transactionally;
4. bind its ID and actor to the model authorization and usage reservation.

Receipts are single-use and bind provider, model, operation, data class, input
kind, content route, run ID, input hash, output limit, reserved cost, and model
and budget policy versions. A changed prompt or parameter requires a new
approval.

## What an override can do

A valid receipt may override automatic dollar and per-run call ceilings. It
cannot:

- exceed hard input or output token limits;
- authorize an unknown provider, model, endpoint, operation, or accounting
  treatment;
- send unknown-classification data externally;
- send source content not marked `external_allowed`;
- be replayed or used after expiry.

## Future site adapter

A web deployment can issue the same receipt type from its authenticated session
middleware and pass `--approval-receipt-id` or the equivalent internal value.
The model-use gate does not trust a username supplied in model text, source
content, or an ordinary request field.
