#!/bin/bash

smoke_target_for_provider() {
  local provider="$1"
  case "$provider" in
    gcp)
      echo "1.1.1.1"
      ;;
    *)
      echo "8.8.8.8"
      ;;
  esac
}

validate_scamper_smoke_text() {
  local text_file="$1"
  local target="$2"
  local min_hops="${3:-2}"

  if [[ ! "$min_hops" =~ ^[0-9]+$ ]]; then
    echo "invalid smoke-test min hop count: $min_hops" >&2
    return 1
  fi
  if [[ ! -s "$text_file" ]]; then
    echo "smoke-test output is empty: $text_file" >&2
    return 1
  fi
  if ! grep -Eiq "trace|traceroute" "$text_file"; then
    echo "smoke-test output does not look like a traceroute" >&2
    cat "$text_file" >&2
    return 1
  fi
  if ! grep -Fq "$target" "$text_file"; then
    echo "smoke-test output does not mention target $target" >&2
    cat "$text_file" >&2
    return 1
  fi

  local hop_count
  hop_count=$(grep -Ec "^[[:space:]]*[0-9]+[[:space:]]+" "$text_file" || true)
  if (( hop_count < min_hops )); then
    echo "smoke-test traceroute had $hop_count hops; expected at least $min_hops" >&2
    cat "$text_file" >&2
    return 1
  fi

  echo "SCAMPER_SMOKE_OK target=$target hops=$hop_count"
}

run_scamper_smoke_test() {
  local provider="$1"
  local trace_args="$2"

  if [[ "${SCAMPER_SMOKE_TEST:-1}" == "0" ]]; then
    echo "SCAMPER_SMOKE_SKIPPED provider=$provider"
    return 0
  fi

  local target="${SCAMPER_SMOKE_TARGET:-}"
  if [[ -z "$target" ]]; then
    target=$(smoke_target_for_provider "$provider")
  fi

  local min_hops="${SCAMPER_SMOKE_MIN_HOPS:-2}"
  local pps="${SCAMPER_SMOKE_PPS:-1}"
  local smoke_dir="${SCAMPER_SMOKE_DIR:-/tmp/scamper-smoke}"
  mkdir -p "$smoke_dir"

  local target_file="$smoke_dir/${provider}-target.txt"
  local warts_file="$smoke_dir/${provider}-${target}.warts"
  local text_file="$smoke_dir/${provider}-${target}.txt"
  local log_file="$smoke_dir/${provider}-${target}.log"

  printf "%s\n" "$target" > "$target_file"

  if ! command -v scamper >/dev/null 2>&1; then
    echo "scamper is not installed; cannot run smoke test" >&2
    return 1
  fi
  if ! command -v sc_warts2text >/dev/null 2>&1; then
    echo "sc_warts2text is not installed; cannot validate smoke-test warts output" >&2
    return 1
  fi

  echo "Running scamper smoke test for $provider against $target"
  echo "sudo scamper -c \"$trace_args\" -p $pps -f $target_file -o $warts_file -O warts"
  sudo scamper -c "$trace_args" -p "$pps" -f "$target_file" -o "$warts_file" -O warts 2>&1 | tee "$log_file"
  sudo sc_warts2text "$warts_file" > "$text_file"
  validate_scamper_smoke_text "$text_file" "$target" "$min_hops"
}
