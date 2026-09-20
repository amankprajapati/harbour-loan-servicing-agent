#!/usr/bin/env bash
# Phase 12: the single script PLAN.md's "definition of done" describes.
# Starts the service, seeds the database, runs the eval suite clean and
# under each gateway regression, runs the contract checker against the
# live service, runs the defect detectors, and prints one summary.
#
# No GPU, no network required: the default LLM client is the scripted
# planner (see src/harbour/llm_client.py), so every step here runs on a
# plain machine with only the Python dependencies in requirements.txt.
#
# Usage: bash scripts/reproduce.sh   (from anywhere; the script cd's to
# the repo root itself)

set -u
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PORT=8199
BASE_URL="http://127.0.0.1:${PORT}"
DB_PATH="$REPO_ROOT/.reproduce_run/harbour.sqlite"
SERVER_LOG="$REPO_ROOT/.reproduce_run/uvicorn.log"
SERVER_PID=""

mkdir -p "$REPO_ROOT/.reproduce_run"
rm -f "$DB_PATH" "$DB_PATH-wal" "$DB_PATH-shm"

STEP_NAMES=()
STEP_RESULTS=()

record() {
    STEP_NAMES+=("$1")
    STEP_RESULTS+=("$2")
}

cleanup() {
    if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null
        wait "$SERVER_PID" 2>/dev/null
    fi
}
trap cleanup EXIT

echo "== 1/6: offline test suite =="
python -m pytest tests/ -q
record "offline_test_suite" $?

echo
echo "== 2/6: generate published cases =="
python cases/generate_cases.py
record "generate_cases" $?

echo
echo "== 3/6: eval suite (clean) =="
python eval/runner.py
record "eval_clean" $?

echo
echo "== 4/6: gateway regression pack =="
python eval/gateway_regression_runner.py
record "gateway_regressions" $?

echo
echo "== 5/6: defect detectors =="
python detectors/run_detectors.py
record "defect_detectors" $?

echo
echo "== 6/6: contract checker against a live service =="
HARBOUR_DB_PATH="$DB_PATH" PYTHONPATH="$REPO_ROOT/src" \
    python -m uvicorn harbour.service:app --host 127.0.0.1 --port "$PORT" \
    > "$SERVER_LOG" 2>&1 &
SERVER_PID=$!

READY=0
for _ in $(seq 1 30); do
    if curl -s -o /dev/null "$BASE_URL/health"; then
        READY=1
        break
    fi
    sleep 0.5
done

if [ "$READY" -eq 1 ]; then
    python -m contract_check.check --base-url "$BASE_URL" --db-path "$DB_PATH"
    record "contract_check" $?
else
    echo "service never became healthy at $BASE_URL; see $SERVER_LOG"
    record "contract_check" 2
fi

kill "$SERVER_PID" 2>/dev/null
wait "$SERVER_PID" 2>/dev/null
SERVER_PID=""

echo
echo "================ reproduce.sh summary ================"
OVERALL=0
for i in "${!STEP_NAMES[@]}"; do
    if [ "${STEP_RESULTS[$i]}" -eq 0 ]; then
        printf "  [PASS] %s\n" "${STEP_NAMES[$i]}"
    else
        printf "  [FAIL] %s (exit %s)\n" "${STEP_NAMES[$i]}" "${STEP_RESULTS[$i]}"
        OVERALL=1
    fi
done
echo "========================================================"

exit $OVERALL
