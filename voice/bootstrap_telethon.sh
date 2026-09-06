#!/usr/bin/env sh
# bootstrap_telethon.sh -- make telethon importable inside the Node-RED container.
#
# The Node-RED image is deliberately slim: it ships a Python interpreter but no
# pip, and its base marks the environment "externally managed" (PEP 668).
#
# Do NOT solve this by bind-mounting the host's site-packages. The host runs
# Python 3.12 and the container runs 3.14; the compiled cryptography/rsa
# extensions are ABI-specific and the mismatch crashes telethon at import.
# Install natively inside the container instead -- that is what this does.
#
# Idempotent: re-running is a no-op once telethon imports cleanly.
#
# Usage (from the host):
#   docker exec -u root nodered sh /data/bootstrap_telethon.sh
#   docker restart nodered

set -eu

TELETHON_VERSION="${TELETHON_VERSION:-1.42.0}"
PY="${PYTHON_BIN:-python3}"
GET_PIP_URL="https://bootstrap.pypa.io/get-pip.py"

log() { printf '[bootstrap] %s\n' "$*"; }
die() { printf '[bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }

command -v "$PY" >/dev/null 2>&1 || die "no Python interpreter found (looked for '$PY')"
log "using $("$PY" --version 2>&1)"

# Already satisfied? Then stop -- this script is called on every container start.
if "$PY" - <<'PYCHECK' >/dev/null 2>&1
import site, sys
try:
    p = site.getusersitepackages()
    sys.path.append(p if isinstance(p, str) else p[0])
except Exception:
    pass
import telethon
PYCHECK
then
    log "telethon already importable -- nothing to do"
    exit 0
fi

# --- 1. Ensure pip exists -------------------------------------------------
if ! "$PY" -m pip --version >/dev/null 2>&1; then
    log "pip missing, bootstrapping from $GET_PIP_URL"
    TMP_DIR="$(mktemp -d)"
    trap 'rm -rf "$TMP_DIR"' EXIT

    if command -v wget >/dev/null 2>&1; then
        wget -q -O "$TMP_DIR/get-pip.py" "$GET_PIP_URL" || die "wget failed to fetch get-pip.py"
    elif command -v curl >/dev/null 2>&1; then
        curl -fsSL -o "$TMP_DIR/get-pip.py" "$GET_PIP_URL" || die "curl failed to fetch get-pip.py"
    else
        die "neither wget nor curl is available to fetch get-pip.py"
    fi

    # --user keeps us out of the distro-managed site-packages entirely.
    "$PY" "$TMP_DIR/get-pip.py" --user --no-warn-script-location \
        || "$PY" "$TMP_DIR/get-pip.py" --user --break-system-packages --no-warn-script-location \
        || die "get-pip.py failed"
    log "pip installed"
fi

# --- 2. Install telethon --------------------------------------------------
# --break-system-packages is required on PEP 668 bases but is unknown to older
# pips, so fall back to the plain form.
log "installing telethon==$TELETHON_VERSION"
"$PY" -m pip install --user --no-warn-script-location \
        --break-system-packages "telethon==$TELETHON_VERSION" \
    || "$PY" -m pip install --user --no-warn-script-location \
        "telethon==$TELETHON_VERSION" \
    || die "pip install telethon failed"

# --- 3. Verify through the same path shim server_brain.py uses -------------
# pip --user drops the package in a site dir that is NOT on the default
# sys.path for a bare script run. If this check fails, server_brain.py's
# _ensure_user_site_packages() would fail the same way.
"$PY" - <<'PYVERIFY' || die "telethon installed but is not importable via the user site path"
import site, sys
try:
    p = site.getusersitepackages()
    sys.path.append(p if isinstance(p, str) else p[0])
except Exception:
    pass
import telethon
print("[bootstrap] telethon %s importable from %s" % (telethon.__version__, telethon.__file__))
PYVERIFY

log "done"
