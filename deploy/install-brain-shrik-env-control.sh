#!/usr/bin/env bash
# Install the fixed root boundary used by the Codex gateway credential cutover.

set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "ERROR: run this installer once through sudo." >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
HELPER_SOURCE="${SCRIPT_DIR}/brain-shrik-env-control"
SUDOERS_SOURCE="${SCRIPT_DIR}/sudoers/brain-shrik-env-control"
HELPER_TARGET="/usr/local/sbin/brain-shrik-env-control"
SUDOERS_TARGET="/etc/sudoers.d/brain-shrik-env-control"
HELPER_TEMP="${HELPER_TARGET}.new"
SUDOERS_TEMP="${SUDOERS_TARGET}.new"

cleanup() {
  /usr/bin/rm -f -- "${HELPER_TEMP}" "${SUDOERS_TEMP}"
}
trap cleanup EXIT

if [[ ! -f "${HELPER_SOURCE}" || -L "${HELPER_SOURCE}" ]]; then
  echo "ERROR: helper source must be a regular non-symlink file." >&2
  exit 2
fi
if [[ ! -f "${SUDOERS_SOURCE}" || -L "${SUDOERS_SOURCE}" ]]; then
  echo "ERROR: sudoers source must be a regular non-symlink file." >&2
  exit 2
fi

/usr/sbin/visudo -cf "${SUDOERS_SOURCE}"
/usr/bin/install --owner=root --group=root --mode=0755 \
  "${HELPER_SOURCE}" "${HELPER_TEMP}"
/usr/bin/mv -f -- "${HELPER_TEMP}" "${HELPER_TARGET}"
/usr/bin/sync -f "$(dirname -- "${HELPER_TARGET}")"

/usr/bin/install --owner=root --group=root --mode=0440 \
  "${SUDOERS_SOURCE}" "${SUDOERS_TEMP}"
/usr/sbin/visudo -cf "${SUDOERS_TEMP}"
/usr/bin/mv -f -- "${SUDOERS_TEMP}" "${SUDOERS_TARGET}"
/usr/bin/sync -f "$(dirname -- "${SUDOERS_TARGET}")"
/usr/sbin/visudo -cf "${SUDOERS_TARGET}"
"${HELPER_TARGET}" --check

echo "Installed ${HELPER_TARGET} and ${SUDOERS_TARGET}."
