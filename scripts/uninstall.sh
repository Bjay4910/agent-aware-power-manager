#!/usr/bin/env bash
# Uninstaller for agent-aware-power-manager (macOS).
#
# What this script does:
# 1. Releases any active sleep hold so normal macOS sleep is restored.
# 2. Unloads and removes the three LaunchAgents from ~/Library/LaunchAgents.
# 3. Prompts before using sudo to remove /etc/sudoers.d/agent-power.
# 4. Removes local runtime state (~/.agent-power, ~/.local/state/agent-power)
#    and optionally ~/.config/agent-power and .venv.

set -euo pipefail

if [[ "${EUID}" -eq 0 ]]; then
  echo "Error: Please run scripts/uninstall.sh as your normal macOS user (not as root)." >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_UID="$(id -u)"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
SUDOERS_TARGET="/etc/sudoers.d/agent-power"

PLISTS=(
  "${LAUNCH_AGENTS_DIR}/com.agentpower.menubar.plist"
  "${LAUNCH_AGENTS_DIR}/com.agentpower.watchdog.plist"
  "${LAUNCH_AGENTS_DIR}/com.agentpower.guard.plist"
)

echo "==> [1/4] Releasing any active sleep hold"
sudo -n /usr/bin/pmset -a disablesleep 0 >/dev/null 2>&1 || true

echo "==> [2/4] Unloading and removing LaunchAgents"
for plist in "${PLISTS[@]}"; do
  if [[ -f "${plist}" ]]; then
    launchctl bootout "gui/${USER_UID}" "${plist}" >/dev/null 2>&1 || true
    rm -f "${plist}"
    echo "  Removed ${plist}"
  fi
done

echo "==> [3/4] Removing sudoers rule ${SUDOERS_TARGET}"
read -r -p "Use sudo to remove ${SUDOERS_TARGET} if present? [y/N] " confirm
if [[ "${confirm}" =~ ^[Yy]$ ]]; then
  sudo rm -f "${SUDOERS_TARGET}"
  echo "  Removed ${SUDOERS_TARGET}"
else
  echo "  Skipped removing ${SUDOERS_TARGET}"
fi

echo "==> [4/4] Cleaning up local runtime state"
rm -rf "${HOME}/.agent-power" "${HOME}/.local/state/agent-power" "${HOME}/.config/agent-power"

read -r -p "Also remove ${REPO_ROOT}/.venv? [y/N] " remove_venv
if [[ "${remove_venv}" =~ ^[Yy]$ ]]; then
  rm -rf "${REPO_ROOT}/.venv"
  echo "  Removed ${REPO_ROOT}/.venv"
fi

echo "Uninstall complete."
