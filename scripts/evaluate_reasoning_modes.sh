#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 6 || $# -gt 7 ]]; then
  echo "usage: $0 ROOT DERIVATION_ID ANCHOR_LIMIT CONCEPT_ID QUESTION OUTPUT_JSON [HIGH_PROPOSAL_ID]" >&2
  exit 2
fi

runtime_root=$1
derivation_id=$2
anchor_limit=$3
concept_id=$4
question=$5
output_json=$6
existing_high_id=${7:-}
trial_dir=$(mktemp -d -t geas-reasoning-eval.XXXXXXXX)
anchor_json="$trial_dir/anchors.json"
uv_bin=${UV_BIN:-/home/openclaw/.local/bin/uv}
jq_bin=${JQ_BIN:-/usr/bin/jq}

if [[ ! -x "$uv_bin" || ! -x "$jq_bin" ]]; then
  echo "reasoning evaluation requires executable uv and jq paths" >&2
  exit 1
fi

progress() {
  echo "[$(date --iso-8601=seconds)] reasoning-eval $*" >&2
}

progress "selecting $anchor_limit grounded leaf anchors"
"$uv_bin" run geas structure-show "$derivation_id" \
  --root "$runtime_root" \
  --leaf-only \
  --limit "$anchor_limit" >"$anchor_json"

mapfile -t anchor_ids < <("$jq_bin" -r '.anchors[].id' "$anchor_json")
if [[ ${#anchor_ids[@]} -eq 0 ]]; then
  echo "reasoning evaluation selected no anchors" >&2
  exit 1
fi

anchor_args=()
for anchor_id in "${anchor_ids[@]}"; do
  anchor_args+=(--anchor "$anchor_id")
done

run_trial() {
  local effort=$1
  local trial_json="$trial_dir/$effort.json"
  progress "starting effort=$effort anchors=${#anchor_ids[@]} output_tokens=32768"
  "$uv_bin" run geas propose-extraction "$derivation_id" \
    "${anchor_args[@]}" \
    --question "$question" \
    --concept "$concept_id" \
    --provider deepseek_local \
    --root "$runtime_root" \
    --max-output-tokens 32768 \
    --reasoning-effort "$effort" \
    --temperature 0 \
    --seed 0 \
    --timeout 14400 \
    --allow-partial-items >"$trial_json" &
  local trial_pid=$!
  local elapsed=0
  while kill -0 "$trial_pid" 2>/dev/null; do
    sleep 30
    elapsed=$((elapsed + 30))
    if kill -0 "$trial_pid" 2>/dev/null; then
      progress "waiting effort=$effort elapsed_seconds=$elapsed"
    fi
  done
  wait "$trial_pid"
  "$jq_bin" -er '.receipt.proposal.id' "$trial_json"
}

if [[ -n "$existing_high_id" ]]; then
  high_id=$existing_high_id
  progress "resuming completed effort=high proposal=$high_id"
else
  high_id=$(run_trial high)
  progress "completed effort=high proposal=$high_id"
fi
max_id=$(run_trial max)
progress "completed effort=max proposal=$max_id"

"$uv_bin" run geas compare-extractions \
  "$high_id" \
  "$max_id" \
  --root "$runtime_root" >"$output_json"
progress "comparison written to $output_json"
