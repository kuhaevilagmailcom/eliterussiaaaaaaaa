#!/usr/bin/env bash
# MGN VPN / H1Cloud: read-only host diagnostics by default.
# No H1 tokens, user traffic, subscriptions, or VPN configuration are read.
set -Eeuo pipefail

CONF=/etc/sysctl.d/90-mgn-vpn-bbr.conf
BACKUP=/var/lib/mgn-vpn/bbr-previous.conf

usage() {
  cat <<'USAGE'
Usage: bash scripts/h1_speed_audit.sh [--report|--enable-bbr|--rollback-bbr]
  --report         Read-only: inspect VPS network/CPU/TCP state (default).
  --enable-bbr     Explicit root-only opt-in; back up current sysctl values,
                   apply tcp_congestion_control=bbr + default_qdisc=fq.
  --rollback-bbr   Restore original sysctl values and remove only our drop-in.

Run this on each VPN/Xray VPS, NOT on BotHost or your local PC.
BBR is not a speed guarantee; compare real downloads and pings before/after.
This script does not change Xray, firewall, ports, MTU or other sysctls.
USAGE
}

report() {
  echo "== MGN H1 VPS speed audit (read-only) =="
  printf 'Kernel: '; uname -r
  printf 'Uptime/load: '; uptime | sed -E 's/^.* up /up /'
  printf 'CPU cores: '; (nproc || echo unknown) 2>/dev/null
  if command -v free >/dev/null 2>&1; then free -h | head -n 3; fi
  if command -v ip >/dev/null 2>&1; then
    local iface
    iface="$(ip -o -4 route show default 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="dev") {print $(i+1); exit}}' || true)"
    if [[ -n "$iface" ]]; then
      echo "Default interface: $iface"
      ip -s link show dev "$iface" 2>/dev/null | head -n 8 || true
      if command -v tc >/dev/null 2>&1; then
        echo "Active interface queuing discipline:"
        tc qdisc show dev "$iface" 2>/dev/null | head -n 4 || true
      fi
    fi
  fi
  if command -v sysctl >/dev/null 2>&1; then
    for key in net.ipv4.tcp_congestion_control net.ipv4.tcp_available_congestion_control net.core.default_qdisc; do
      sysctl "$key" 2>/dev/null || true
    done
  fi
  if command -v ss >/dev/null 2>&1; then
    echo "TCP socket summary:"
    ss -s 2>/dev/null | head -n 6 || true
  fi
  if command -v systemctl >/dev/null 2>&1; then
    for service in xray xray-core sing-box; do
      state="$(systemctl is-active "$service" 2>/dev/null || true)"
      if [[ "$state" == "active" ]]; then echo "$service: active"; fi
    done
  fi
  echo "NOTE: BotHost-to-node ping is not user VPN speed."
  echo "Compare an actual client's route, packet loss and download speed for each country."
}

require_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run this mode with sudo/root on the VPN VPS." >&2
    exit 1
  fi
  command -v sysctl >/dev/null 2>&1 || { echo "sysctl not found" >&2; exit 1; }
}

enable_bbr() {
  require_root
  if [[ -e "$CONF" || -e "$BACKUP" ]]; then
    echo "Already configured/backed up. Review or use --rollback-bbr first." >&2
    exit 1
  fi
  if command -v modprobe >/dev/null 2>&1; then
    modprobe tcp_bbr 2>/dev/null || true
  fi
  local available old_cc old_qdisc
  available="$(sysctl -n net.ipv4.tcp_available_congestion_control)"
  if [[ " $available " != *" bbr "* ]]; then
    echo "Kernel does not expose TCP BBR; no changes made." >&2
    exit 1
  fi
  old_cc="$(sysctl -n net.ipv4.tcp_congestion_control)"
  old_qdisc="$(sysctl -n net.core.default_qdisc)"
  # Only allow well-formed, previously read sysctl identifiers in backup.
  [[ "$old_cc" =~ ^[a-z_0-9]+$ && "$old_qdisc" =~ ^[a-z_0-9]+$ ]] || {
    echo "Unexpected sysctl values; refusing to modify." >&2
    exit 1
  }
  umask 077
  install -d -m 700 /var/lib/mgn-vpn
  printf 'net.ipv4.tcp_congestion_control=%s\nnet.core.default_qdisc=%s\n' "$old_cc" "$old_qdisc" > "$BACKUP"
  cat > "$CONF" <<'CONFIG'
# MGN VPN opt-in BBR/fq. Use h1_speed_audit.sh --rollback-bbr to restore.
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
CONFIG
  if ! sysctl -w net.core.default_qdisc=fq >/dev/null ||
     ! sysctl -w net.ipv4.tcp_congestion_control=bbr >/dev/null; then
    echo "Failed to apply BBR; rolling back automatically." >&2
    sysctl -w "net.ipv4.tcp_congestion_control=$old_cc" >/dev/null || true
    sysctl -w "net.core.default_qdisc=$old_qdisc" >/dev/null || true
    rm -f -- "$CONF" "$BACKUP"
    exit 1
  fi
  echo "BBR/fq enabled for NEW TCP connections. Existing interface qdisc may differ."
  echo "Use --rollback-bbr if client-side speed/latency gets worse."
  report
}

rollback_bbr() {
  require_root
  if [[ ! -f "$CONF" || ! -f "$BACKUP" ]]; then
    echo "MGN backup and drop-in not both present; refusing destructive rollback." >&2
    exit 1
  fi
  local old_cc old_qdisc
  old_cc="$(sed -n 's/^net.ipv4.tcp_congestion_control=//p' "$BACKUP")"
  old_qdisc="$(sed -n 's/^net.core.default_qdisc=//p' "$BACKUP")"
  [[ "$old_cc" =~ ^[a-z_0-9]+$ && "$old_qdisc" =~ ^[a-z_0-9]+$ ]] || {
    echo "Bad backup format. Refusing to restore." >&2
    exit 1
  }
  sysctl -w "net.ipv4.tcp_congestion_control=$old_cc" >/dev/null
  sysctl -w "net.core.default_qdisc=$old_qdisc" >/dev/null
  rm -f -- "$CONF" "$BACKUP"
  echo "Restored: TCP congestion=$old_cc, default qdisc=$old_qdisc."
  report
}

case "${1:---report}" in
  --report) report ;;
  --enable-bbr) enable_bbr ;;
  --rollback-bbr) rollback_bbr ;;
  --help|-h) usage ;;
  *) usage >&2; exit 2 ;;
esac
