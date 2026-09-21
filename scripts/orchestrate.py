"""
Orchestrate multi-annotator document assignments.

Reads an annotation config (YAML) and generates balanced per-annotator
assignment manifests, supporting either one global redundancy level
or mixed redundancy tiers (for example, a higher-redundancy validation subset).

Usage:
    python scripts/orchestrate.py                            # default config
    python scripts/orchestrate.py --config my_config.yaml    # custom config
"""

import argparse
import glob
import json
import os
import random
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml


BACKEND_DIR = Path(__file__).resolve().parent.parent / "web" / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from prepare_comparison_data import parse_events_json, serialize_event_for_comparison


def _is_int_like(value: Any) -> bool:
    """Return True when value is an int but not a bool."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    """Return True when value is numeric but not a bool."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_redundancy(name: str, redundancy: int, annotator_count: int):
    """Validate a redundancy setting against the annotator pool."""
    if not _is_int_like(redundancy):
        print(f"ERROR: {name} must be an integer.")
        sys.exit(1)

    if redundancy < 1:
        print(f"ERROR: {name} must be >= 1.")
        sys.exit(1)

    if redundancy > annotator_count:
        print(
            f"ERROR: {name} ({redundancy}) cannot exceed the number of annotators ({annotator_count})."
        )
        sys.exit(1)


def load_config(config_path: str) -> dict:
    """Load and validate the annotation config YAML."""
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # Validate required keys
    required_keys = ["annotators", "redundancy", "ground_truth_dir", "model_tags_dir", "output_dir"]
    for key in required_keys:
        if key not in config:
            print(f"ERROR: Missing required key '{key}' in config.")
            sys.exit(1)

    annotators = config["annotators"]
    redundancy = config["redundancy"]
    validation_redundancy = config.get("validation_redundancy", redundancy)
    document_limit = config.get("document_limit")
    validation_document_count = config.get("validation_document_count")
    validation_document_percentage = config.get("validation_document_percentage")

    if not annotators or len(annotators) == 0:
        print("ERROR: At least one annotator must be defined.")
        sys.exit(1)

    _validate_redundancy("Redundancy", redundancy, len(annotators))
    _validate_redundancy("Validation redundancy", validation_redundancy, len(annotators))

    if document_limit is not None and (not _is_int_like(document_limit) or document_limit < 1):
        print("ERROR: document_limit must be an integer >= 1 when provided.")
        sys.exit(1)

    if validation_document_count is not None and (
        not _is_int_like(validation_document_count) or validation_document_count < 0
    ):
        print("ERROR: validation_document_count must be an integer >= 0 when provided.")
        sys.exit(1)

    if validation_document_percentage is not None and (
        not _is_number(validation_document_percentage)
        or validation_document_percentage < 0
        or validation_document_percentage > 100
    ):
        print("ERROR: validation_document_percentage must be between 0 and 100.")
        sys.exit(1)

    if validation_document_count is not None and validation_document_percentage is not None:
        print(
            "ERROR: Use either validation_document_count or validation_document_percentage, not both."
        )
        sys.exit(1)

    config["validation_redundancy"] = validation_redundancy
    return config


def find_common_documents(ground_truth_dir: str, model_tags_dir: str) -> list[str]:
    """Find documents that exist in both GT and model directories."""
    # Handle nested dir convention
    nested = os.path.join(ground_truth_dir, "ground_truth_isi")
    if os.path.exists(nested):
        ground_truth_dir = nested

    gt_files = {os.path.basename(f) for f in glob.glob(os.path.join(ground_truth_dir, "*.json"))}
    model_files = {os.path.basename(f) for f in glob.glob(os.path.join(model_tags_dir, "*.json"))}

    common = sorted(gt_files.intersection(model_files))
    return common


def _normalize_events_for_comparison(events: list[dict], schema: dict | None) -> set[str]:
    """Build comparable event signatures for assignment-time discrepancy checks."""
    signatures = set()
    for event in events:
        signature = serialize_event_for_comparison(event, schema)
        if signature:
            signatures.add(signature)
        else:
            filtered = {k: v for k, v in event.items() if k != "event_id"}
            signatures.add(json.dumps(filtered, sort_keys=True, ensure_ascii=False))
    return signatures


def find_documents_with_discrepancies(
    ground_truth_dir: str,
    model_tags_dir: str,
    schema: dict | None = None,
) -> list[str]:
    """Return only the common documents whose event sets differ."""
    # Handle nested dir convention
    nested = os.path.join(ground_truth_dir, "ground_truth_isi")
    if os.path.exists(nested):
        ground_truth_dir = nested

    discrepant_documents: list[str] = []
    for filename in find_common_documents(ground_truth_dir, model_tags_dir):
        gt_events = parse_events_json(os.path.join(ground_truth_dir, filename))
        model_events = parse_events_json(os.path.join(model_tags_dir, filename))

        gt_signatures = _normalize_events_for_comparison(gt_events, schema)
        model_signatures = _normalize_events_for_comparison(model_events, schema)

        if gt_signatures != model_signatures:
            discrepant_documents.append(filename)

    return discrepant_documents


def _get_weights(annotators: list[dict]) -> list[float]:
    """Extract and normalize weights from annotator configs.

    If no weight is specified, defaults to 1.0 (equal distribution).
    Returns a list of normalized weights that sum to 1.0.
    """
    raw_weights = [a.get("weight", 1.0) for a in annotators]
    total = sum(raw_weights)
    if total <= 0:
        # Fallback to equal weights
        n = len(annotators)
        return [1.0 / n] * n
    return [w / total for w in raw_weights]


def order_documents_for_assignment(
    documents: list[str],
    strategy: str,
    seed: int = 42,
) -> list[str]:
    """Return documents in the order used for sampling/assignment."""
    ordered_documents = list(documents)
    if strategy == "random":
        random.Random(seed).shuffle(ordered_documents)
    elif strategy != "sequential":
        raise ValueError(f"Unknown assignment strategy '{strategy}'. Use 'random' or 'sequential'.")
    return ordered_documents


def select_documents_for_assignment(
    documents: list[str],
    document_limit: int | None,
    strategy: str,
    seed: int = 42,
) -> list[str]:
    """Select the subset of documents that will be handled in this run."""
    ordered_documents = order_documents_for_assignment(documents, strategy=strategy, seed=seed)
    if document_limit is None:
        return ordered_documents
    return ordered_documents[:document_limit]


def resolve_validation_document_count(
    total_selected_documents: int,
    validation_document_count: int | None = None,
    validation_document_percentage: float | None = None,
) -> int:
    """Resolve how many selected documents should belong to the validation tier."""
    if total_selected_documents <= 0:
        return 0

    if validation_document_count is not None:
        return min(validation_document_count, total_selected_documents)

    if validation_document_percentage is None or validation_document_percentage <= 0:
        return 0

    resolved_count = int((total_selected_documents * validation_document_percentage) / 100 + 0.5)
    if resolved_count == 0:
        resolved_count = 1
    return min(resolved_count, total_selected_documents)


def split_documents_for_validation(
    selected_documents: list[str],
    validation_document_count: int,
) -> tuple[list[str], list[str]]:
    """Split the selected documents into validation and standard tiers."""
    validation_documents = list(selected_documents[:validation_document_count])
    standard_documents = list(selected_documents[validation_document_count:])
    return validation_documents, standard_documents


def assign_documents_random(
    documents: list[str],
    annotators: list[dict],
    redundancy: int,
    seed: int = 42,
) -> dict[str, list[str]]:
    """Assign documents to annotators using weighted random assignment.

    Each document is assigned to exactly `redundancy` annotators.
    Annotators with higher weight receive proportionally more documents.
    When all weights are equal, this degrades to balanced round-robin.

    Args:
        documents: List of document filenames.
        annotators: List of annotator dicts with 'id' and optional 'weight'.
        redundancy: Number of annotators per document.
        seed: Random seed for reproducibility.

    Returns:
        Dict mapping annotator_id -> list of assigned document filenames.
    """
    document_plan = [
        (doc_id, redundancy)
        for doc_id in order_documents_for_assignment(documents, strategy="random", seed=seed)
    ]
    return assign_documents_by_plan(document_plan, annotators)


def assign_documents_sequential(
    documents: list[str],
    annotators: list[dict],
    redundancy: int,
) -> dict[str, list[str]]:
    """Assign documents to annotators in deterministic sequential order.

    Same as random but without shuffling — documents are assigned
    in their sorted order. Supports weights.

    Args:
        documents: List of document filenames (already sorted).
        annotators: List of annotator dicts with 'id' and optional 'weight'.
        redundancy: Number of annotators per document.

    Returns:
        Dict mapping annotator_id -> list of assigned document filenames.
    """
    document_plan = [
        (doc_id, redundancy)
        for doc_id in order_documents_for_assignment(documents, strategy="sequential")
    ]
    return assign_documents_by_plan(document_plan, annotators)


def assign_documents_by_plan(
    document_plan: list[tuple[str, int]],
    annotators: list[dict],
) -> dict[str, list[str]]:
    """Assign documents using a per-document redundancy plan.

    Uses a deficit-based greedy approach: for each document, pick the
    requested number of annotators whose current assignment count is furthest
    below their target share.

    Target share for annotator i = (total_assignments * weight_i)
    where total_assignments is the sum of the requested redundancies.
    """
    annotator_ids = [a["id"] for a in annotators]
    weights = _get_weights(annotators)
    total_assignments = sum(doc_redundancy for _, doc_redundancy in document_plan)

    # Target number of docs per annotator
    targets = [w * total_assignments for w in weights]

    # Initialize
    assignments: dict[str, list[str]] = {aid: [] for aid in annotator_ids}
    counts = [0] * len(annotator_ids)

    for doc, doc_redundancy in document_plan:
        if doc_redundancy < 1 or doc_redundancy > len(annotator_ids):
            raise ValueError(
                f"Invalid redundancy {doc_redundancy} for document '{doc}'. "
                f"Expected a value between 1 and {len(annotator_ids)}."
            )

        # Compute deficit: how far below target each annotator is
        # Higher deficit = should get more docs
        deficits = [(targets[i] - counts[i], i) for i in range(len(annotator_ids))]
        # Sort by deficit descending, pick top requested annotators
        deficits.sort(key=lambda x: -x[0])
        chosen = [deficits[j][1] for j in range(doc_redundancy)]

        for idx in chosen:
            assignments[annotator_ids[idx]].append(doc)
            counts[idx] += 1

    return assignments


def print_assignment_summary(
    assignments: dict[str, list[str]],
    annotators: list[dict],
    document_plan: list[tuple[str, int]],
):
    """Print a summary of the assignment distribution."""
    name_map = {a["id"]: a.get("name", a["id"]) for a in annotators}
    weights = _get_weights(annotators)
    total_docs = len(document_plan)
    total_assignments = sum(doc_redundancy for _, doc_redundancy in document_plan)
    redundancy_tiers: dict[int, int] = defaultdict(int)
    for _, doc_redundancy in document_plan:
        redundancy_tiers[doc_redundancy] += 1

    print("\n" + "=" * 60)
    print("  ASSIGNMENT SUMMARY")
    print("=" * 60)
    print(f"  Total documents:    {total_docs}")
    print(f"  Total assignments:  {total_assignments}")
    print(f"  Annotators:         {len(annotators)}")
    print("  Redundancy tiers:")
    for doc_redundancy in sorted(redundancy_tiers):
        print(f"    {redundancy_tiers[doc_redundancy]:4d} docs x redundancy {doc_redundancy}")
    print("-" * 60)

    for i, (aid, docs) in enumerate(assignments.items()):
        pct = weights[i] * 100
        target = weights[i] * total_assignments
        print(f"  {name_map[aid]:15s}  {pct:5.1f}%  ->  {len(docs):4d} docs  (target: {target:.0f})")

    # Compute overlap statistics
    doc_to_annotators: dict[str, list[str]] = defaultdict(list)
    for aid, docs in assignments.items():
        for doc in docs:
            doc_to_annotators[doc].append(aid)

    overlap_counts = defaultdict(int)
    for doc, anns in doc_to_annotators.items():
        overlap_counts[len(anns)] += 1

    print("-" * 60)
    print("  Overlap distribution:")
    for count in sorted(overlap_counts.keys()):
        print(f"    {count} annotators: {overlap_counts[count]} documents")
    print("=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Generate multi-annotator document assignments."
    )
    parser.add_argument(
        "--config",
        default="annotation_config.yaml",
        help="Path to annotation config YAML (default: annotation_config.yaml)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing assignments if they exist.",
    )
    args = parser.parse_args()

    # Resolve config path relative to project root
    config_path = args.config
    if not os.path.isabs(config_path):
        # Try relative to CWD first, then relative to script location
        if not os.path.exists(config_path):
            script_dir = Path(__file__).resolve().parent.parent
            config_path = str(script_dir / config_path)

    if not os.path.exists(config_path):
        print(f"ERROR: Config file not found: {args.config}")
        sys.exit(1)

    config = load_config(config_path)
    project_root = Path(config_path).resolve().parent

    # Resolve paths relative to project root
    gt_dir = str(project_root / config["ground_truth_dir"])
    model_dir = str(project_root / config["model_tags_dir"])
    output_dir = str(project_root / config["output_dir"])
    schema = None
    schema_file = config.get("schema_file")
    if schema_file:
        schema_path = project_root / schema_file
        if not schema_path.exists():
            print(f"ERROR: Schema file not found: {schema_path}")
            sys.exit(1)
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = yaml.safe_load(f)

    annotators = config["annotators"]
    redundancy = config["redundancy"]
    validation_redundancy = config["validation_redundancy"]
    strategy = config.get("assignment_strategy", "random")
    seed = config.get("seed", 42)
    document_limit = config.get("document_limit")
    validation_document_count = config.get("validation_document_count")
    validation_document_percentage = config.get("validation_document_percentage")

    if strategy not in {"random", "sequential"}:
        print(f"ERROR: Unknown assignment strategy '{strategy}'. Use 'random' or 'sequential'.")
        sys.exit(1)

    # Find common documents
    common_documents = find_common_documents(gt_dir, model_dir)
    if not common_documents:
        print("ERROR: No common documents found between GT and model directories.")
        sys.exit(1)

    documents = find_documents_with_discrepancies(gt_dir, model_dir, schema=schema)
    if not documents:
        print("ERROR: No discrepant documents found. Nothing requires annotation.")
        sys.exit(1)

    if document_limit is not None and document_limit > len(documents):
        print(
            f"WARNING: document_limit ({document_limit}) exceeds available discrepant documents "
            f"({len(documents)}). Using all available discrepant documents."
        )

    selected_documents = select_documents_for_assignment(
        documents,
        document_limit=document_limit,
        strategy=strategy,
        seed=seed,
    )
    resolved_validation_document_count = resolve_validation_document_count(
        len(selected_documents),
        validation_document_count=validation_document_count,
        validation_document_percentage=validation_document_percentage,
    )
    validation_documents, standard_documents = split_documents_for_validation(
        selected_documents,
        resolved_validation_document_count,
    )
    validation_document_set = set(validation_documents)
    document_plan = [
        (doc_id, validation_redundancy if doc_id in validation_document_set else redundancy)
        for doc_id in selected_documents
    ]

    print(f"Found {len(common_documents)} common documents.")
    print(f"Found {len(documents)} documents with discrepancies to assign.")
    print(f"Selected {len(selected_documents)} documents for this run.")
    print(
        "Validation tier: "
        f"{len(validation_documents)} docs at redundancy {validation_redundancy}"
    )
    print(
        "Standard tier:   "
        f"{len(standard_documents)} docs at redundancy {redundancy}"
    )
    print(f"Strategy: {strategy} | Annotators: {len(annotators)}")

    # Check if assignments already exist
    assignments_dir = os.path.join(output_dir, "assignments")
    if os.path.exists(assignments_dir) and not args.force:
        existing = [f for f in os.listdir(assignments_dir) if f.endswith(".json")]
        if existing:
            print(f"\nWARNING: Assignments already exist in {assignments_dir}")
            print(f"  Found: {', '.join(existing)}")
            print("  Use --force to overwrite.")
            sys.exit(1)

    # Generate assignments
    assignments = assign_documents_by_plan(document_plan, annotators)

    # Print summary
    print_assignment_summary(assignments, annotators, document_plan)

    # Create output directory structure
    os.makedirs(assignments_dir, exist_ok=True)
    results_dir = os.path.join(output_dir, "results")
    integration_dir = os.path.join(output_dir, "integration")
    os.makedirs(integration_dir, exist_ok=True)

    for annotator in annotators:
        aid = annotator["id"]
        os.makedirs(os.path.join(results_dir, aid), exist_ok=True)

    # Save per-annotator manifests
    for aid, docs in assignments.items():
        manifest_path = os.path.join(assignments_dir, f"{aid}.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(sorted(docs), f, indent=2)
        print(f"Saved assignment manifest: {manifest_path}")

    plan_path = os.path.join(output_dir, "assignment_plan.json")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "strategy": strategy,
                "seed": seed,
                "document_limit": document_limit,
                "selected_documents": selected_documents,
                "validation_documents": validation_documents,
                "standard_documents": standard_documents,
                "standard_redundancy": redundancy,
                "validation_redundancy": validation_redundancy,
                "validation_document_count_requested": validation_document_count,
                "validation_document_percentage_requested": validation_document_percentage,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )
    print(f"Saved assignment plan: {plan_path}")

    # Save a snapshot of the config used
    snapshot_path = os.path.join(output_dir, "config_snapshot.yaml")
    shutil.copy2(config_path, snapshot_path)
    print(f"Saved config snapshot: {snapshot_path}")

    print("\nOrchestration complete! Annotators can now run:")
    print("   ./run_annotator.ps1 <annotator_id>    (Windows)")
    print("   ./run_annotator.sh <annotator_id>     (Linux/Mac)")


if __name__ == "__main__":
    main()
