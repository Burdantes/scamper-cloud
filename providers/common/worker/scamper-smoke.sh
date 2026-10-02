#!/bin/bash

smoke_target_for_provider() {
  local provider="$1"
  local address_family="${2:-4}"
  if [[ "$address_family" == "6" ]]; then
    echo "2606:4700:4700::1111"
    return
  fi
  case "$provider" in
    gcp)
      echo "1.1.1.1"
      ;;
    *)
      echo "8.8.8.8"
      ;;
  esac
}

log_ipv6_network_state() {
  local target="$1"

  echo "IPv6 address configuration:"
  ip -6 address show 2>&1 || true
  echo "IPv6 route configuration:"
  ip -6 route show table all 2>&1 || true
  echo "IPv6 route selected for $target:"
  ip -6 route get "$target" 2>&1 || true

  if command -v curl >/dev/null 2>&1; then
    echo "IPv6 public address check:"
    curl -6 --fail --silent --show-error --max-time 10 \
      https://api64.ipify.org 2>&1 || true
    echo
  fi
}

configure_azure_ipv6_measurement_firewall() {
  if ! command -v ip6tables >/dev/null 2>&1; then
    echo "ip6tables is required before opening Azure's IPv6 NSG workaround" >&2
    return 1
  fi

  # Azure NSGs cannot select ICMPv6, so the Azure driver admits IPv6 traffic
  # to this subnet with protocol=Any. Enforce the narrow measurement policy
  # in the guest before probing: ICMPv6 plus replies to outbound connections,
  # with all other unsolicited IPv6 input dropped.
  sudo ip6tables -C INPUT -i lo -j ACCEPT 2>/dev/null || \
    sudo ip6tables -I INPUT 1 -i lo -j ACCEPT
  sudo ip6tables -C INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || \
    sudo ip6tables -I INPUT 2 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  sudo ip6tables -C INPUT -p ipv6-icmp -j ACCEPT 2>/dev/null || \
    sudo ip6tables -I INPUT 3 -p ipv6-icmp -j ACCEPT
  sudo ip6tables -P INPUT DROP
  echo "AZURE_IPV6_GUEST_FIREWALL_READY"
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
  # sc_warts2text prints numbered "*" rows for unanswered TTLs.  Counting
  # those rows let an IPv6 worker with no connectivity pass the smoke test.
  # Require actual responding-hop addresses instead.
  hop_count=$(grep -Ec "^[[:space:]]*[0-9]+[[:space:]]+[^*[:space:]]" "$text_file" || true)
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
  local address_family="${3:-4}"

  if [[ "${SCAMPER_SMOKE_TEST:-1}" == "0" ]]; then
    echo "SCAMPER_SMOKE_SKIPPED provider=$provider"
    return 0
  fi

  local target="${SCAMPER_SMOKE_TARGET:-}"
  if [[ -z "$target" ]]; then
    target=$(smoke_target_for_provider "$provider" "$address_family")
  fi

  local min_hops="${SCAMPER_SMOKE_MIN_HOPS:-2}"
  local pps="${SCAMPER_SMOKE_PPS:-1}"
  local smoke_dir="${SCAMPER_SMOKE_DIR:-/tmp/scamper-smoke}"
  mkdir -p "$smoke_dir"

  local target_file="$smoke_dir/${provider}-target.txt"
  local family_label="ipv${address_family}"
  local warts_file="$smoke_dir/${provider}-${family_label}.warts"
  local text_file="$smoke_dir/${provider}-${family_label}.txt"
  local log_file="$smoke_dir/${provider}-${family_label}.log"

  printf "%s\n" "$target" > "$target_file"

  if ! command -v scamper >/dev/null 2>&1; then
    echo "scamper is not installed; cannot run smoke test" >&2
    return 1
  fi
  if ! command -v sc_warts2text >/dev/null 2>&1; then
    echo "sc_warts2text is not installed; cannot validate smoke-test warts output" >&2
    return 1
  fi

  if [[ "$address_family" == "6" ]]; then
    if [[ "$provider" == "azr" ]]; then
      configure_azure_ipv6_measurement_firewall
    fi
    # Preserve the network state in the worker log even when the traceroute
    # smoke test fails.  This keeps live cloud retries bounded while making it
    # possible to distinguish a missing route/public mapping from filtered
    # ICMPv6 responses after the VM has been torn down.
    log_ipv6_network_state "$target"
  fi

  echo "Running scamper smoke test for $provider against $target"
  echo "sudo scamper -c \"$trace_args\" -p $pps -f $target_file -o $warts_file -O warts"
  sudo scamper -c "$trace_args" -p "$pps" -f "$target_file" -o "$warts_file" -O warts 2>&1 | tee "$log_file"
  sudo sc_warts2text "$warts_file" > "$text_file"
  validate_scamper_smoke_text "$text_file" "$target" "$min_hops"
}
