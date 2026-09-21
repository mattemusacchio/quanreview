#!/usr/bin/env bash
# Launch the annotation UI on the bundled synthetic sample corpus.
#
# This is the entry point for anyone evaluating the tool: it needs no data of
# your own and no configuration. Results are written to data/sample_run/ and
# can be deleted freely.
#
#   ./run_demo.sh
#
# macOS/Linux. On Windows use run_demo.cmd.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SAMPLE_DIR="${SCRIPT_DIR}/data/sample"
RUN_DIR="${SCRIPT_DIR}/data/sample_run"

find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
    elif [ -x "${HOME}/.local/bin/uv" ]; then
        echo "${HOME}/.local/bin/uv"
    elif [ -x "${HOME}/.cargo/bin/uv" ]; then
        echo "${HOME}/.cargo/bin/uv"
    fi
}

UV="$(find_uv || true)"
if [ -z "${UV}" ]; then
    echo "ERROR: uv is required but was not found on PATH."
    echo "  Install it from https://docs.astral.sh/uv/ and run this script again."
    exit 1
fi

# The sample corpus is generated, not committed, so a fresh clone builds it on
# first run. Regenerating is cheap and deterministic.
if [ ! -d "${SAMPLE_DIR}/ground_truth" ]; then
    echo "Generating the sample corpus..."
    (cd "${SCRIPT_DIR}" && "${UV}" run python ./scripts/make_sample_data.py)
fi

mkdir -p "${RUN_DIR}"

# Point the backend at the sample corpus's own source texts rather than the
# project-wide location, which a fresh clone does not have.
export NER_SOURCE_TEXT_DIR="${SAMPLE_DIR}/source"

echo "Starting the annotation UI on the sample corpus..."
echo "  corpus:  ${SAMPLE_DIR}"
echo "  results: ${RUN_DIR}"
echo

# Stay at the repository root. web/backend has a pyproject.toml of its own,
# declared in poetry non-package mode, so running uv from inside it makes uv
# try to build that project and fail. Python still resolves the backend's local
# imports because it puts the script's own directory on sys.path.
cd "${SCRIPT_DIR}"
exec "${UV}" run python ./web/backend/web_app.py \
    --ground-truth-dir "${SAMPLE_DIR}/ground_truth" \
    --model-tags-dir "${SAMPLE_DIR}/model_tags" \
    --output-file "${RUN_DIR}/output.json" \
    --corrected-dir "${RUN_DIR}/corrected" \
    --schema "${SCRIPT_DIR}/comparison_schema.yaml"
