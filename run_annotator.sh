#!/usr/bin/env bash
# Multi-Annotator Launch Script
# Usage: ./run_annotator.sh <annotator_id>
# Example: ./run_annotator.sh annotator_a

set -euo pipefail

ANNOTATOR="${1:-}"
PROJECT_DIR="${2:-data/annotation_project}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR}"
BACKEND_DIR="${REPO_ROOT}/web/backend"
SCHEMA_PATH="${REPO_ROOT}/comparison_schema.yaml"
GROUND_TRUTH_DIR="${REPO_ROOT}/data/sample/ground_truth"
MODEL_TAGS_DIR="${REPO_ROOT}/data/sample/model_tags"
VENV_PYTHON="${REPO_ROOT}/.venv/bin/python"

find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi

    # The official macOS/Linux installer puts uv here, but a terminal opened
    # before installation may not have this directory on PATH yet.
    if [ -x "${HOME}/.local/bin/uv" ]; then
        printf '%s\n' "${HOME}/.local/bin/uv"
        return 0
    fi

    echo "ERROR: uv was not found." >&2
    echo "  Install it with:" >&2
    echo "    curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    echo "  Then reopen the terminal, or run:" >&2
    echo '    export PATH="$HOME/.local/bin:$PATH"' >&2
    exit 1
}

if [ -z "$ANNOTATOR" ]; then
    echo "Usage: ./run_annotator.sh <annotator_id> [project_dir]"
    echo "Example: ./run_annotator.sh annotator_a"
    exit 1
fi

if [[ "$PROJECT_DIR" = /* ]]; then
    RESOLVED_PROJECT_DIR="$PROJECT_DIR"
else
    RESOLVED_PROJECT_DIR="${REPO_ROOT}/${PROJECT_DIR}"
fi

ASSIGNMENT_FILE="${RESOLVED_PROJECT_DIR}/assignments/${ANNOTATOR}.json"

if [ ! -f "$ASSIGNMENT_FILE" ]; then
    echo "ERROR: No assignment found for annotator '${ANNOTATOR}'."
    echo "  Expected: ${ASSIGNMENT_FILE}"
    echo "  Run 'uv run python scripts/orchestrate.py' first."
    exit 1
fi

if [ -x "$VENV_PYTHON" ]; then
    PYTHON_CMD=("$VENV_PYTHON")
else
    UV_BIN="$(find_uv)"
    PYTHON_CMD=("$UV_BIN" run python)
fi

DOC_COUNT=$("${PYTHON_CMD[@]}" -c 'import json, sys; print(len(json.load(open(sys.argv[1], encoding="utf-8"))))' "$ASSIGNMENT_FILE")

echo ""
echo "========================================"
echo "  NER Tag Validator - Annotator Mode"
echo "========================================"
echo "  Annotator:  ${ANNOTATOR}"
echo "  Documents:  ${DOC_COUNT} assigned"
echo "========================================"
echo ""

OUTPUT_FILE="${RESOLVED_PROJECT_DIR}/results/${ANNOTATOR}/output.json"
CORRECTED_DIR="${RESOLVED_PROJECT_DIR}/results/${ANNOTATOR}"

if [ -x "$VENV_PYTHON" ]; then
    (
        cd "$BACKEND_DIR" || exit 1
        "$VENV_PYTHON" ./web_app.py \
            --ground-truth-dir "$GROUND_TRUTH_DIR" \
            --model-tags-dir "$MODEL_TAGS_DIR" \
            --output-file "$OUTPUT_FILE" \
            --corrected-dir "$CORRECTED_DIR" \
            --schema "$SCHEMA_PATH" \
            --annotator "${ANNOTATOR}" \
            --project-dir "$RESOLVED_PROJECT_DIR"
    )
else
    (
        cd "$REPO_ROOT" || exit 1
        "${UV_BIN:-$(find_uv)}" run python ./web/backend/web_app.py \
            --ground-truth-dir "$GROUND_TRUTH_DIR" \
            --model-tags-dir "$MODEL_TAGS_DIR" \
            --output-file "$OUTPUT_FILE" \
            --corrected-dir "$CORRECTED_DIR" \
            --schema "$SCHEMA_PATH" \
            --annotator "${ANNOTATOR}" \
            --project-dir "$RESOLVED_PROJECT_DIR"
    )
fi
