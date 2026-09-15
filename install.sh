#!/bin/sh
set -eu
if [ "$(uname -s)" != "Darwin" ]; then
  printf '%s\n' "This installer targets macOS. Use install.ps1 on Windows." >&2
  exit 1
fi
usage() {
  printf '%s\n' "Usage: sh install.sh HTTPS_SERVER [--web-only] [--skip-activation] [--skip-codex-registration] [--python /path/python3] [--manual-start] [--no-python-download]" >&2
}
if [ "$#" -lt 1 ]; then usage; exit 1; fi
CLIENT_SERVER=$1
shift
CLIENT_ACTIVATE=1
CLIENT_REGISTER=1
CLIENT_STARTUP=1
CLIENT_DOWNLOAD=1
CLIENT_BASE_PYTHON=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --web-only) CLIENT_ACTIVATE=0; CLIENT_REGISTER=0 ;;
    --skip-activation) CLIENT_ACTIVATE=0 ;;
    --skip-codex-registration) CLIENT_REGISTER=0 ;;
    --manual-start) CLIENT_STARTUP=0 ;;
    --no-python-download) CLIENT_DOWNLOAD=0 ;;
    --python) shift; if [ "$#" -lt 1 ]; then usage; exit 1; fi; CLIENT_BASE_PYTHON=$1 ;;
    *) usage; exit 1 ;;
  esac
  shift
done
CLIENT_SOURCE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/quant_agent_mcp"
CLIENT_INSTALL="$HOME/Library/Application Support/QuantAgentClient/python"
CLIENT_PYTHON="$CLIENT_INSTALL/bin/python"
CLIENT_TEMP_ROOT="${TMPDIR:-/tmp}"
CLIENT_TEMP_ROOT="${CLIENT_TEMP_ROOT%/}"
CLIENT_STAGE="$(mktemp -d "$CLIENT_TEMP_ROOT/quant-agent-install.XXXXXXXX")"
cleanup() {
  case "$CLIENT_STAGE" in
    "$CLIENT_TEMP_ROOT"/quant-agent-install.*) rm -rf -- "$CLIENT_STAGE" ;;
  esac
}
trap cleanup EXIT HUP INT TERM
if [ ! -f "$CLIENT_SOURCE/pyproject.toml" ]; then
  printf '%s\n' "Use the owner's complete public client release. No private repository is required." >&2
  exit 1
fi
if [ -x "$CLIENT_PYTHON" ]; then
  CLIENT_BASE_PYTHON=$CLIENT_PYTHON
elif [ -z "$CLIENT_BASE_PYTHON" ]; then
  CLIENT_BASE_PYTHON="$(command -v python3 || true)"
fi
if [ -z "$CLIENT_BASE_PYTHON" ] || ! "$CLIENT_BASE_PYTHON" -I -B -c 'import sys; assert sys.version_info >= (3,11)' 2>/dev/null; then
  if [ "$CLIENT_DOWNLOAD" -ne 1 ]; then
    printf '%s\n' "Install Python 3.11+ from https://www.python.org/downloads/macos/ and rerun with --python its full path." >&2
    exit 1
  fi
  CLIENT_PKG="$CLIENT_STAGE/python-3.13.15-macos11.pkg"
  printf '%s\n' "Python is missing. Downloading a verified python.org installer. macOS Installer will request its normal system confirmation."
  /usr/bin/curl --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 --output "$CLIENT_PKG" "https://www.python.org/ftp/python/3.13.15/python-3.13.15-macos11.pkg"
  CLIENT_HASH="$(/usr/bin/shasum -a 256 "$CLIENT_PKG" | /usr/bin/awk '{print $1}')"
  if [ "$CLIENT_HASH" != "3b7eaf7f29825f796e8267024435540ddf1f17fc9a97ad58095daa7a75bfdcd3" ]; then
    printf '%s\n' "Python installer SHA256 mismatch; nothing was opened." >&2; exit 1
  fi
  CLIENT_SIGNATURE="$(/usr/sbin/pkgutil --check-signature "$CLIENT_PKG")"
  case "$CLIENT_SIGNATURE" in
    *"Developer ID Installer: Python Software Foundation"*) ;;
    *) printf '%s\n' "Python installer signer mismatch; nothing was opened." >&2; exit 1 ;;
  esac
  /usr/sbin/spctl --assess --type install "$CLIENT_PKG"
  /usr/bin/open -W -a Installer "$CLIENT_PKG"
  CLIENT_BASE_PYTHON="/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13"
  if ! "$CLIENT_BASE_PYTHON" -I -B -c 'import sys; assert sys.version_info >= (3,11)'; then
    printf '%s\n' "Finish the official Python installation, then rerun this client installer." >&2; exit 1
  fi
fi
if [ ! -x "$CLIENT_PYTHON" ]; then
  "$CLIENT_BASE_PYTHON" -I -B -m venv "$CLIENT_INSTALL"
fi
mkdir -p "$CLIENT_STAGE/package/quant_agent_mcp"
cp "$CLIENT_SOURCE/pyproject.toml" "$CLIENT_STAGE/package/"
for CLIENT_FILE in __init__.py config.py credentials.py client.py server.py device_bridge.py cli.py; do
  cp "$CLIENT_SOURCE/quant_agent_mcp/$CLIENT_FILE" "$CLIENT_STAGE/package/quant_agent_mcp/"
done
"$CLIENT_PYTHON" -I -B -m pip install --disable-pip-version-check --no-cache-dir "$CLIENT_STAGE/package"
"$CLIENT_PYTHON" -I -B -m pip check
if [ "$CLIENT_STARTUP" -eq 1 ]; then
  "$CLIENT_PYTHON" -I -B -m quant_agent_mcp.cli configure --server "$CLIENT_SERVER" --start-on-login --start
else
  "$CLIENT_PYTHON" -I -B -m quant_agent_mcp.cli configure --server "$CLIENT_SERVER" --start
fi
if [ "$CLIENT_ACTIVATE" -eq 1 ]; then
  "$CLIENT_PYTHON" -I -B -m quant_agent_mcp.cli activate
fi
if [ "$CLIENT_REGISTER" -eq 1 ]; then
  if ! "$CLIENT_PYTHON" -I -B -m quant_agent_mcp.cli register-codex; then
    printf '%s\n' "Automatic Codex registration is incomplete. Add the STDIO command below in Codex Settings > MCP servers."
  fi
fi
"$CLIENT_PYTHON" -I -B -m quant_agent_mcp.cli status
if [ "$CLIENT_ACTIVATE" -eq 0 ] && [ "$CLIENT_REGISTER" -eq 0 ]; then
  printf '%s\n' "Web device component is ready. Enter your invitation on the configured website; the MCP slot was not activated."
else
  printf '%s\n' "STDIO command: $CLIENT_PYTHON" "Arguments: -I -B -m quant_agent_mcp.server"
  printf '%s\n' "Restart the quant-agent MCP connection in Codex after registration."
fi
if [ "$CLIENT_STARTUP" -eq 0 ]; then
  printf '%s\n' "Automatic login startup was skipped. Run the client start-device command after each OS login."
fi
