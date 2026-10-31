#!/usr/bin/env bash
# Phase D: Linux bridge verification runner (ADR 0041/0043/0044/0045).
# Run on a Linux machine with bubblewrap installed. Nothing here has been
# executed by the authoring agent; every result is [unverified] until pasted back.
set -euo pipefail

echo "== Linux bridge verification =="
echo "bwrap: $(command -v bwrap || echo MISSING)"
echo

echo "== 1) pytest kit (D1 loopback, D2 outbound block, D3 real containment, D4 readonly) =="
uv run pytest packages/nervos-core/tests/integration/test_linux_bridge_verification.py -v
echo

echo "== 2) full real-host launch suite (A1) =="
uv run pytest apps/worker/tests/integration/test_real_host_process_launch.py -v
echo

echo "== 3) real-socket vendor + tool-choice/multi-turn suites =="
uv run pytest apps/worker/tests/integration/test_real_e2e_shim_vendor_routes.py \
  apps/worker/tests/integration/test_real_e2e_shim_tool_choice_multiturn.py \
  apps/worker/tests/integration/test_real_e2e_shim_tools_stream.py \
  apps/worker/tests/integration/test_real_e2e_shim_child.py -v
echo

echo "== 4) Python SDK smoke =="
uv run python scripts/bridge_sdk_smoke.py --sdk-python <PATH_TO_THROWAWAY_VENV_PYTHON>
echo

echo "== 5) Node SDK smoke (optional) =="
# uv run python scripts/bridge_sdk_smoke.py --sdk-python <...> --node-dir <throwaway-node-dir>
echo
echo "== Paste the full output back; nothing is 'verified' until then. =="
