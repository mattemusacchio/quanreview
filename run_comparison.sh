#!/bin/bash

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT" || exit 1

SCHEMA="comparison_schema.yaml"
GT_DIR="data/sample/ground_truth"
MODEL_DIR="data/sample/model_tags"
OUTPUT_FILE="data/comparison_output.json"
CORRECTED_DIR="data/corrected_ground_truth"

echo "Starting NER Validator web app..."
echo "Comparing data. Verified tags will be saved in 'corrected_ground_truth'."
uv run python web/backend/web_app.py "$GT_DIR" "$MODEL_DIR" "$OUTPUT_FILE" "$CORRECTED_DIR" "$SCHEMA"
