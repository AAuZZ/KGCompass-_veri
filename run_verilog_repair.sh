#!/bin/bash
set -e

export PYTHONPATH=$(pwd)
export PYTHONIOENCODING=${PYTHONIOENCODING:-utf-8}
export PYTHONUTF8=${PYTHONUTF8:-1}
export BENCHMARK_NAME="verilog-local"
export LOCAL_BENCHMARK_PATH=${LOCAL_BENCHMARK_PATH:-"$(pwd)/benchmarks/verilog_repair_cases.json"}
export VERILOG_REPAIR_MAX_ATTEMPTS=${VERILOG_REPAIR_MAX_ATTEMPTS:-3}
export VERILOG_ALLOW_UNVALIDATED=${VERILOG_ALLOW_UNVALIDATED:-0}
export VERILOG_GENERATION_MAX_ATTEMPTS=${VERILOG_GENERATION_MAX_ATTEMPTS:-3}
export VERILOG_DEBUG_MAX_ATTEMPTS=${VERILOG_DEBUG_MAX_ATTEMPTS:-3}
export PYTHONUNBUFFERED=${PYTHONUNBUFFERED:-1}

INSTANCE_ID=${1:-verilog_demo__uart_idle-0001}
RUN_MODEL_NAME=${RUN_MODEL_NAME:-deepseek}
FORCE_RERUN=${FORCE_RERUN:-1}
CONDA_ENV_NAME=${CONDA_ENV_NAME:-kgcompass}
if [ -n "${PYTHON_BIN:-}" ]; then
  PYTHON_CMD=("$PYTHON_BIN")
elif command -v conda >/dev/null 2>&1; then
  PYTHON_CMD=(conda run -n "$CONDA_ENV_NAME" python)
else
  PYTHON_CMD=(python)
fi
REPO_IDENTIFIER=${INSTANCE_ID%-*}
SOURCE_REPOS_DIR=${VERILOG_SOURCE_REPOS_DIR:-${VERILOG_REPOS_DIR:-"verilog_repair_cases"}}
SOURCE_REPO_PATH="${SOURCE_REPOS_DIR}/${REPO_IDENTIFIER}"
WORK_ROOT=${VERILOG_WORK_ROOT:-"workdirs"}
WORK_RUN_ROOT="${WORK_ROOT}/${INSTANCE_ID}_${RUN_MODEL_NAME}"
WORK_REPOS_DIR="${WORK_RUN_ROOT}/repos"
WORK_REPO_PATH="${WORK_REPOS_DIR}/${REPO_IDENTIFIER}"
export VERILOG_REPOS_DIR="$WORK_REPOS_DIR"

if [ ! -d "$SOURCE_REPO_PATH/.git" ]; then
  echo "ERROR: Source Verilog repo '$SOURCE_REPO_PATH' not found or is not a git repository." >&2
  exit 1
fi

if [ -n "$(git -C "$SOURCE_REPO_PATH" status --short)" ]; then
  echo "ERROR: Source Verilog repo '$SOURCE_REPO_PATH' has local changes. Refusing to run on a dirty source repo." >&2
  git -C "$SOURCE_REPO_PATH" status --short >&2
  exit 1
fi

RUN_DIR="tests/${INSTANCE_ID}_${RUN_MODEL_NAME}"
KG_LOCATIONS_DIR="${RUN_DIR}/kg_locations"
LLM_LOCATIONS_DIR="${RUN_DIR}/llm_locations"
FINAL_LOCATIONS_DIR="${RUN_DIR}/final_locations"
PATCH_DIR="${RUN_DIR}/patches"
RUN_LOG="${RUN_DIR}/run.log"
SUMMARY_FILE="${RUN_DIR}/repair_summary.json"
REPAIR_PROGRESS_LOG="${RUN_DIR}/repair_progress.log"

if [ "$FORCE_RERUN" = "1" ]; then
  rm -rf "$RUN_DIR" "$WORK_RUN_ROOT"
fi

mkdir -p "$RUN_DIR"
: > "$RUN_LOG"
exec > >(tee -a "$RUN_LOG") 2>&1

mkdir -p "$WORK_REPOS_DIR"
if [ ! -d "$WORK_REPO_PATH/.git" ]; then
  git clone --quiet "$SOURCE_REPO_PATH" "$WORK_REPO_PATH"
fi

mkdir -p "$KG_LOCATIONS_DIR" "$LLM_LOCATIONS_DIR" "$FINAL_LOCATIONS_DIR" "$PATCH_DIR"

log_stage() {
  echo
  echo "================================================="
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
  echo "================================================="
}

log_stage "KGCompass Verilog repair started"
echo "Instance: $INSTANCE_ID"
echo "Model tag: $RUN_MODEL_NAME"
echo "Source repository: $SOURCE_REPO_PATH"
echo "Working repository: $WORK_REPO_PATH"
echo "Benchmark: $LOCAL_BENCHMARK_PATH"
echo "Generation attempts: $VERILOG_GENERATION_MAX_ATTEMPTS"
echo "Debug attempts: $VERILOG_DEBUG_MAX_ATTEMPTS"
echo "Allow unvalidated final patch: $VERILOG_ALLOW_UNVALIDATED"
echo "Run directory: $RUN_DIR"
echo "Run log: $RUN_LOG"
echo "Repair progress log: $REPAIR_PROGRESS_LOG"
echo "Python command: ${PYTHON_CMD[*]}"
echo "Force rerun: $FORCE_RERUN"

KG_RESULT_FILE="${KG_LOCATIONS_DIR}/${INSTANCE_ID}.json"
if [ -f "$KG_RESULT_FILE" ]; then
  log_stage "Stage 1/4: KG localization skipped"
  echo "Existing KG location: $KG_RESULT_FILE"
else
  log_stage "Stage 1/4: building KG and running KG localization"
  "${PYTHON_CMD[@]}" kgcompass/fl.py "$INSTANCE_ID" "$REPO_IDENTIFIER" "$KG_LOCATIONS_DIR" verilog-local
  echo "KG location saved to $KG_RESULT_FILE"
fi

FINAL_RESULT_FILE="${FINAL_LOCATIONS_DIR}/${INSTANCE_ID}.json"
if [ -f "$FINAL_RESULT_FILE" ]; then
  log_stage "Stage 2/3: final localization skipped"
  echo "Existing final location: $FINAL_RESULT_FILE"
else
  log_stage "Stage 2/3: using KG localization as final localization"
  cp "$KG_RESULT_FILE" "$FINAL_RESULT_FILE"
  echo "Final location copied from KG location to $FINAL_RESULT_FILE"
fi

PATCH_FILE="${PATCH_DIR}/${INSTANCE_ID}.patch"
if [ -f "$PATCH_FILE" ]; then
  log_stage "Stage 3/3: repair skipped"
  echo "Existing final patch: $PATCH_FILE"
else
  log_stage "Stage 3/3: running generation/debug repair loop"
  : > "$REPAIR_PROGRESS_LOG"
  export KGCOMPASS_REPAIR_PROGRESS_LOG="$REPAIR_PROGRESS_LOG"
  echo "Repair progress will also be written to: $REPAIR_PROGRESS_LOG"
  "${PYTHON_CMD[@]}" kgcompass/repair.py "$FINAL_LOCATIONS_DIR" \
    --instance_id "$INSTANCE_ID" \
    --playground_dir "$WORK_REPOS_DIR" \
    --repo_identifier "$REPO_IDENTIFIER"
  echo "Repair loop finished."
  if [ -f "$PATCH_FILE" ]; then
    echo "Validated final patch: $PATCH_FILE"
    ls -l "$PATCH_FILE"
  else
    echo "No validated final patch was produced."
    if [ -f "$SUMMARY_FILE" ]; then
      echo "Repair summary: $SUMMARY_FILE"
    fi
  fi
fi

log_stage "KGCompass Verilog repair finished"
echo "Instance: $INSTANCE_ID"
echo "Artifacts: $RUN_DIR"
if [ -n "$(git -C "$SOURCE_REPO_PATH" status --short)" ]; then
  echo "ERROR: Source repository was modified during the run: $SOURCE_REPO_PATH" >&2
  git -C "$SOURCE_REPO_PATH" status --short >&2
  exit 1
fi
echo "Source repository remained clean: $SOURCE_REPO_PATH"
echo "================================================="
