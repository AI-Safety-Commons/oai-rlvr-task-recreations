#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
skip_build=false

usage() {
  cat <<'EOF'
Usage: ./setup.sh [--skip-build]

Prepare the Inspect environment and the LLM-governed HTTP gateway.

Environment:
  OPENROUTER_API_KEY         OpenRouter key used by the policy model
  POLICY_API_KEY             Optional override for OPENROUTER_API_KEY
  POLICY_BASE_URL            API base (default: https://openrouter.ai/api/v1)
  POLICY_PROVIDER            Optional OpenRouter provider slug (no fallbacks)
  POLICY_MODEL               OpenRouter model ID (default: openai/gpt-5.6-luna)
  POLICY_CACHE_TTL_SECONDS   Default accepted-response cache TTL (86400)
  POLICY_CACHE_MAX_ENTRIES   Maximum cached responses (1000)
  POLICY_CACHE_MAX_BODY_BYTES Maximum bytes in one cached response (8388608)
  GATEWAY_STATE_DIR          Durable logs/cache/simulation state directory
EOF
}

while (($#)); do
  case "$1" in
    --skip-build) skip_build=true ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; printf 'Unknown option: %s\n' "$1" >&2; exit 1 ;;
  esac
  shift
done

command -v docker >/dev/null || { echo "Docker is required." >&2; exit 1; }
docker compose version >/dev/null 2>&1 || {
  echo "Docker Compose v2 is required." >&2
  exit 1
}

python_bin="${PYTHON:-python3}"
"${python_bin}" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' || {
  echo "Python 3.11 or newer is required." >&2
  exit 1
}

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  "${python_bin}" -m venv "${VENV_DIR}"
fi
"${VENV_DIR}/bin/python" -m pip install --editable "${SCRIPT_DIR}[dev]"

mkdir -p "${GATEWAY_STATE_DIR:-${SCRIPT_DIR}/gateway-state}"
docker compose --project-directory "${SCRIPT_DIR}" \
  -f "${SCRIPT_DIR}/compose-shared.yaml" config --quiet

if [[ "${skip_build}" == false ]]; then
  docker compose --project-directory "${SCRIPT_DIR}" \
    -f "${SCRIPT_DIR}/compose-shared.yaml" build
fi

task_list="$(cd "${SCRIPT_DIR}" && "${VENV_DIR}/bin/inspect" list tasks)"
grep -q 'fast_follow' <<<"${task_list}" || {
  echo "Inspect did not register the fast-follow task." >&2
  exit 1
}

cat <<EOF
Setup complete.

Set OPENROUTER_API_KEY (or POLICY_API_KEY), then run:
  cd ${SCRIPT_DIR}
  GATEWAY_STATE_DIR='${GATEWAY_STATE_DIR:-${SCRIPT_DIR}/gateway-state}' \\
    .venv/bin/inspect eval --run-config run.yaml

Gateway audit logs and persistent simulation/cache state:
  ${GATEWAY_STATE_DIR:-${SCRIPT_DIR}/gateway-state}
EOF
