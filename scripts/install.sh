#!/usr/bin/env bash
# Installer for agent-aware-power-manager (macOS).
#
# What this script does:
# 1. Creates a local Python virtual environment (.venv) and installs `agent-power`.
# 2. Writes and loads three user LaunchAgents in ~/Library/LaunchAgents:
#    - com.agentpower.menubar.plist  (menu-bar app at login)
#    - com.agentpower.watchdog.plist (orphan-hold watchdog every 60 seconds)
#    - com.agentpower.guard.plist    (notify-first runaway script guard every 60 seconds)
# 3. Shows you the exact single-line sudoers rule and asks for confirmation before
#    using sudo to validate it with `visudo -cf` and install it at:
#    /etc/sudoers.d/agent-power

set -euo pipefail

if [[ "${EUID}" -eq 0 ]]; then
  echo "Error: Please run scripts/install.sh as your normal macOS user (not as root)." >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CURRENT_USER="$(id -un)"
USER_UID="$(id -u)"
VENV_DIR="${REPO_ROOT}/.venv"
PYTHON_BIN="${VENV_DIR}/bin/python"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
STATE_DIR="${HOME}/.agent-power"
CONFIG_DIR="${HOME}/.config/agent-power"
LOG_DIR="${HOME}/.local/state/agent-power"
SUDOERS_TARGET="/etc/sudoers.d/agent-power"

MENUBAR_PLIST="${LAUNCH_AGENTS_DIR}/com.agentpower.menubar.plist"
WATCHDOG_PLIST="${LAUNCH_AGENTS_DIR}/com.agentpower.watchdog.plist"
GUARD_PLIST="${LAUNCH_AGENTS_DIR}/com.agentpower.guard.plist"

echo "==> [1/4] Setting up Python virtual environment at ${VENV_DIR}"
python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --upgrade pip
"${VENV_DIR}/bin/pip" install -e "${REPO_ROOT}"

echo "==> [2/4] Creating local state, config, and log directories"
mkdir -p "${STATE_DIR}" "${CONFIG_DIR}" "${LOG_DIR}" "${LAUNCH_AGENTS_DIR}"

if [[ ! -f "${CONFIG_DIR}/allow" ]]; then
  cat > "${CONFIG_DIR}/allow" <<'EOF'
# Substring allowlist for the runaway background script guard.
# Any process whose command line contains one of these non-empty lines is ignored.
EOF
fi

echo "==> [3/4] Writing LaunchAgents to ${LAUNCH_AGENTS_DIR}"

cat > "${MENUBAR_PLIST}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.agentpower.menubar</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON_BIN}</string>
    <string>-m</string>
    <string>agent_power.menubar</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${REPO_ROOT}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ProcessType</key>
  <string>Interactive</string>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/menubar.out.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/menubar.err.log</string>
</dict>
</plist>
EOF

cat > "${WATCHDOG_PLIST}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.agentpower.watchdog</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON_BIN}</string>
    <string>-m</string>
    <string>agent_power.watchdog</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${REPO_ROOT}</string>
  <key>StartInterval</key>
  <integer>60</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/watchdog.out.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/watchdog.err.log</string>
</dict>
</plist>
EOF

cat > "${GUARD_PLIST}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.agentpower.guard</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON_BIN}</string>
    <string>-m</string>
    <string>agent_power.runaway_guard</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${REPO_ROOT}</string>
  <key>StartInterval</key>
  <integer>60</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/guard.out.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/guard.err.log</string>
</dict>
</plist>
EOF

echo "==> [4/4] Configuring narrow passwordless sudo rule for pmset disablesleep"
TMP_SUDOERS="$(mktemp "${TMPDIR:-/tmp}/agent-power-sudoers.XXXXXX")"
trap 'rm -f "${TMP_SUDOERS}"' EXIT

cat > "${TMP_SUDOERS}" <<EOF
# Managed by agent-aware-power-manager (scripts/install.sh)
# Grants user '${CURRENT_USER}' passwordless permission to run ONLY:
#   /usr/bin/pmset -a disablesleep 0
#   /usr/bin/pmset -a disablesleep 1
# No other commands or arguments are permitted by this rule.
${CURRENT_USER} ALL=(root) NOPASSWD: /usr/bin/pmset -a disablesleep 0, /usr/bin/pmset -a disablesleep 1
EOF

echo ""
echo "The following sudoers file will be validated with 'visudo -cf' and installed"
echo "to ${SUDOERS_TARGET}:"
echo "------------------------------------------------------------------------"
cat "${TMP_SUDOERS}"
echo "------------------------------------------------------------------------"
read -r -p "Proceed with sudo to validate and install ${SUDOERS_TARGET}? [y/N] " confirm
if [[ ! "${confirm}" =~ ^[Yy]$ ]]; then
  echo "Aborted sudoers installation. LaunchAgents were written but not loaded."
  exit 1
fi

sudo /usr/sbin/visudo -cf "${TMP_SUDOERS}"
sudo install -o root -g wheel -m 0440 "${TMP_SUDOERS}" "${SUDOERS_TARGET}"
sudo /usr/sbin/visudo -cf "${SUDOERS_TARGET}"

echo "==> Loading LaunchAgents for gui/${USER_UID}"
for plist in "${MENUBAR_PLIST}" "${WATCHDOG_PLIST}" "${GUARD_PLIST}"; do
  launchctl bootout "gui/${USER_UID}" "${plist}" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/${USER_UID}" "${plist}"
done

echo ""
echo "Installation complete!"
echo "  CLI binary: ${VENV_DIR}/bin/agent-power"
echo "  Check status: ${VENV_DIR}/bin/agent-power status"
