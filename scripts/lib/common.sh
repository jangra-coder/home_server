#!/usr/bin/env bash
# common.sh -- shared helpers for the home_server scripts.
#
# Source this, do not execute it:
#     . "$(dirname "$0")/../lib/common.sh"
#
# Everything here exists to make the destructive scripts hard to misfire:
# dry-run is the default, targets must be named explicitly, and the disk that
# currently boots the machine is fenced off.
# shellcheck shell=bash

# --- Output ---------------------------------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_RESET=$'\033[0m'; C_RED=$'\033[31m'; C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'; C_BLUE=$'\033[34m'; C_BOLD=$'\033[1m'
else
    C_RESET=''; C_RED=''; C_GREEN=''; C_YELLOW=''; C_BLUE=''; C_BOLD=''
fi

info()  { printf '%s==>%s %s\n' "$C_BLUE"   "$C_RESET" "$*"; }
ok()    { printf '%s ok %s %s\n' "$C_GREEN"  "$C_RESET" "$*"; }
warn()  { printf '%swarn%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()   { printf '%serr %s %s\n' "$C_RED"    "$C_RESET" "$*" >&2; }
die()   { err "$*"; exit 1; }
hr()    { printf '%s\n' "------------------------------------------------------------"; }

header() {
    printf '\n%s%s%s\n' "$C_BOLD" "$*" "$C_RESET"
    hr
}

# --- Dry-run plumbing -----------------------------------------------------
# DRY_RUN=1 (default) prints commands instead of running them. Scripts flip it
# to 0 only when the operator passes --apply.
DRY_RUN="${DRY_RUN:-1}"
ASSUME_YES="${ASSUME_YES:-0}"

# run <cmd> [args...] -- execute, or print when dry-running.
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  %s[dry-run]%s %s\n' "$C_YELLOW" "$C_RESET" "$*"
    else
        printf '  %s[run]%s %s\n' "$C_GREEN" "$C_RESET" "$*"
        "$@"
    fi
}

# run_sh <shell string> -- same, for pipelines/redirection that need a shell.
run_sh() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  %s[dry-run]%s sh -c %s\n' "$C_YELLOW" "$C_RESET" "$1"
    else
        printf '  %s[run]%s sh -c %s\n' "$C_GREEN" "$C_RESET" "$1"
        sh -c "$1"
    fi
}

dry_run_banner() {
    if [ "$DRY_RUN" -eq 1 ]; then
        warn "DRY RUN -- nothing will be changed. Re-run with --apply to execute."
    else
        warn "APPLY MODE -- changes will be written to disk."
    fi
}

# --- Guards ---------------------------------------------------------------
require_cmd() {
    for cmd in "$@"; do
        command -v "$cmd" >/dev/null 2>&1 || die "required command not found: $cmd"
    done
}

have_cmd() { command -v "$1" >/dev/null 2>&1; }

require_root() {
    [ "$(id -u)" -eq 0 ] || die "this script must run as root (try: sudo $0 $*)"
}

# confirm <prompt> -- demands the literal word YES. Honours ASSUME_YES=1.
confirm() {
    local prompt="${1:-Proceed?}" reply
    if [ "$ASSUME_YES" -eq 1 ]; then
        warn "ASSUME_YES=1, auto-confirming: $prompt"
        return 0
    fi
    printf '%s%s%s [type YES to continue]: ' "$C_BOLD" "$prompt" "$C_RESET"
    read -r reply || return 1
    [ "$reply" = "YES" ]
}

# --- Disk helpers ---------------------------------------------------------

# root_disk -- the whole-disk device that currently backs /, e.g. "sda".
root_disk() {
    local src parent
    src="$(findmnt -no SOURCE / 2>/dev/null)" || return 1
    [ -n "$src" ] || return 1
    parent="$(lsblk -no PKNAME "$src" 2>/dev/null | head -n1 | tr -d ' ')"
    if [ -n "$parent" ]; then
        printf '%s\n' "$parent"
    else
        # Already a whole disk (or an unusual source such as an overlay).
        basename "$src"
    fi
}

# normalise_disk /dev/sdb -> sdb
normalise_disk() { basename "${1#/dev/}"; }

# assert_block_device /dev/sdb
assert_block_device() {
    local dev="$1"
    [ -n "$dev" ] || die "no device given"
    [ -b "$dev" ] || die "not a block device: $dev"
}

# assert_not_system_disk /dev/sdb -- refuse to touch the booted disk.
assert_not_system_disk() {
    local target rootd
    target="$(normalise_disk "$1")"
    rootd="$(root_disk || true)"
    if [ -z "$rootd" ]; then
        warn "could not determine the disk backing / -- proceeding without that guard"
        return 0
    fi
    if [ "$target" = "$rootd" ]; then
        die "refusing to operate on $1: it is the disk currently mounted at / ($rootd)"
    fi
    ok "target $1 is not the running system disk (/ is on $rootd)"
}

# assert_unmounted /dev/sdb -- refuse if the disk or any partition is mounted.
assert_unmounted() {
    local dev="$1" mounted
    mounted="$(lsblk -nro NAME,MOUNTPOINT "$dev" 2>/dev/null | awk 'NF>1 {print $1" -> "$2}')"
    if [ -n "$mounted" ]; then
        err "device $dev has mounted partitions:"
        printf '    %s\n' "$mounted" >&2
        die "unmount them first (sudo umount ...) before continuing"
    fi
    ok "no partition of $dev is currently mounted"
}

# show_device /dev/sdb -- print model, size and current layout for confirmation.
show_device() {
    local dev="$1"
    header "Target device: $dev"
    lsblk -o NAME,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINT,MODEL "$dev" 2>/dev/null \
        || warn "lsblk could not read $dev"
    if have_cmd smartctl; then
        smartctl -i "$dev" 2>/dev/null | grep -Ei 'model|serial|capacity' | sed 's/^/    /' || true
    fi
    hr
}

# disk_free_bytes <path>
disk_free_bytes() { df -B1 --output=avail "$1" 2>/dev/null | tail -n1 | tr -d ' '; }

# dir_size_bytes <path>
dir_size_bytes() { du -sb "$1" 2>/dev/null | awk '{print $1}'; }

human() {
    local bytes="${1:-0}"
    if have_cmd numfmt; then
        numfmt --to=iec --suffix=B "$bytes" 2>/dev/null || printf '%sB' "$bytes"
    else
        awk -v b="$bytes" 'BEGIN{
            split("B KB MB GB TB PB", u, " ");
            i=1; while (b>=1024 && i<6) { b/=1024; i++ }
            printf "%.1f%s", b, u[i]
        }'
    fi
}

# parse_common_flags "$@" -- consumes --apply/--yes/--help style flags.
# Sets DRY_RUN / ASSUME_YES and echoes the remaining arguments.
parse_common_flags() {
    local rest=()
    while [ $# -gt 0 ]; do
        case "$1" in
            --apply)   DRY_RUN=0 ;;
            --dry-run) DRY_RUN=1 ;;
            --yes|-y)  ASSUME_YES=1 ;;
            *)         rest+=("$1") ;;
        esac
        shift
    done
    printf '%s\n' "${rest[@]+"${rest[@]}"}"
}
