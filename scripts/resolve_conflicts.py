"""
Resolve post-IAA conflicts produced by ``scripts/integrate_annotations.py``.

The workflow is:
1. Run integration to generate ``integration/conflicts/<doc_id>.json`` bundles.
2. Inspect the conflict bundle(s).
3. Resolve a document by either:
   - choosing an existing annotator output with ``--source-annotator``
   - providing a custom merged JSON list with ``--resolution-file``

The script writes:
- ``integration/resolved/<doc_id>.json`` for the final chosen output
- ``integration/conflict_resolution_report.json`` as the follow-up artifact
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from scripts.integrate_annotations import resolve_config_path
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts.integrate_annotations import resolve_config_path


def load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_json_list(path: Path) -> list[Any]:
    data = load_json(path)
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON list in {path}")
    return data


def get_integration_paths(config_path: str) -> dict[str, Path]:
    resolved_config_path = resolve_config_path(config_path)
    project_root = resolved_config_path.parent
    config = load_json_or_yaml(resolved_config_path)
    output_dir = (project_root / config["output_dir"]).resolve()
    integration_dir = output_dir / "integration"
    return {
        "config_path": resolved_config_path,
        "output_dir": output_dir,
        "integration_dir": integration_dir,
        "conflicts_dir": integration_dir / "conflicts",
        "resolved_dir": integration_dir / "resolved",
        "report_path": integration_dir / "conflict_resolution_report.json",
    }


def load_json_or_yaml(path: Path) -> dict[str, Any]:
    import yaml

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise ValueError(f"Expected a mapping in {path}")

    return data


def list_conflict_docs(config_path: str) -> list[str]:
    paths = get_integration_paths(config_path)
    if not paths["conflicts_dir"].exists():
        return []
    return sorted(path.name for path in paths["conflicts_dir"].glob("*.json"))


def resolve_conflict(
    config_path: str,
    doc_id: str,
    *,
    source_annotator: str | None = None,
    resolution_file: str | None = None,
    reviewer: str = "manual-reviewer",
    note: str | None = None,
) -> dict[str, Any]:
    if bool(source_annotator) == bool(resolution_file):
        raise ValueError("Provide exactly one of --source-annotator or --resolution-file.")

    paths = get_integration_paths(config_path)
    conflict_bundle_path = paths["conflicts_dir"] / doc_id
    if not conflict_bundle_path.exists():
        raise FileNotFoundError(f"Conflict bundle not found: {conflict_bundle_path}")

    bundle = load_json(conflict_bundle_path)
    candidates = bundle.get("candidates", [])

    if source_annotator:
        matching_candidate = next(
            (candidate for candidate in candidates if candidate["annotator_id"] == source_annotator),
            None,
        )
        if not matching_candidate:
            raise ValueError(
                f"Annotator '{source_annotator}' is not available for {doc_id}. "
                f"Available: {[candidate['annotator_id'] for candidate in candidates]}"
            )
        resolved_events = matching_candidate["events"]
        resolution_method = "annotator_output"
    else:
        resolved_events = load_json_list(Path(resolution_file))
        resolution_method = "custom_resolution_file"

    paths["resolved_dir"].mkdir(parents=True, exist_ok=True)
    resolved_output_path = paths["resolved_dir"] / doc_id
    with open(resolved_output_path, "w", encoding="utf-8") as f:
        json.dump(resolved_events, f, indent=2, ensure_ascii=False)

    resolution_report = {
        "config_path": str(paths["config_path"]),
        "integration_dir": str(paths["integration_dir"]),
        "resolved_documents": [],
    }
    if paths["report_path"].exists():
        existing_report = load_json(paths["report_path"])
        if isinstance(existing_report, dict):
            resolution_report.update(existing_report)

    resolved_documents = [
        entry
        for entry in resolution_report.get("resolved_documents", [])
        if entry.get("doc_id") != doc_id
    ]
    resolved_documents.append(
        {
            "doc_id": doc_id,
            "resolved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "resolution_method": resolution_method,
            "source_annotator": source_annotator,
            "resolution_file": str(Path(resolution_file).resolve()) if resolution_file else None,
            "reviewer": reviewer,
            "note": note,
            "conflict_bundle": str(conflict_bundle_path.resolve()),
            "resolved_output": str(resolved_output_path.resolve()),
        }
    )
    resolution_report["resolved_documents"] = sorted(
        resolved_documents,
        key=lambda entry: entry["doc_id"],
    )

    with open(paths["report_path"], "w", encoding="utf-8") as f:
        json.dump(resolution_report, f, indent=2, ensure_ascii=False)

    return {
        "doc_id": doc_id,
        "resolved_output": str(resolved_output_path.resolve()),
        "resolution_report": str(paths["report_path"].resolve()),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Resolve post-IAA conflicts using generated conflict bundles."
    )
    parser.add_argument(
        "--config",
        default="annotation_config.yaml",
        help="Path to annotation config YAML (default: annotation_config.yaml)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List unresolved conflict bundle documents and exit.",
    )
    parser.add_argument("--doc-id", help="Document ID to resolve (e.g. conflict.json)")
    parser.add_argument(
        "--source-annotator",
        help="Resolve by copying an annotator's output from the conflict bundle.",
    )
    parser.add_argument(
        "--resolution-file",
        help="Resolve by supplying a custom merged JSON list.",
    )
    parser.add_argument(
        "--reviewer",
        default="manual-reviewer",
        help="Reviewer name/id for the resolution report.",
    )
    parser.add_argument("--note", default=None, help="Optional note for the resolution report.")
    args = parser.parse_args()

    if args.list:
        for doc_id in list_conflict_docs(args.config):
            print(doc_id)
        return

    if not args.doc_id:
        parser.error("--doc-id is required unless --list is used.")

    result = resolve_conflict(
        args.config,
        args.doc_id,
        source_annotator=args.source_annotator,
        resolution_file=args.resolution_file,
        reviewer=args.reviewer,
        note=args.note,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
