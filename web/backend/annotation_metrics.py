"""Who-won metrics over annotator outputs (GT vs Model vs correction).

Each saved annotation is traced back to the discrepant pair it came from by
reproducing the reviewer app's exact pairing (:class:`NERValidatorCore`):
perfect ground-truth/model matches and real-only events are auto-kept and
never shown to the annotator, model-only events are dropped, and only
overlapping discrepant pairs are presented. Reversing that with a naive
quantity heuristic mis-assigns events whenever a value repeats in a document,
so this module goes through the same core the reviewer uses.

Every field is read from the comparison schema, so nothing about the label set
is hard-coded. Fields can be excluded from the metrics ("banned") without
altering the pairing, and annotators are discovered from the ``<name>_out.json``
files in the annotations directory.

Per matched pair, each field falls into one bucket:

``both_agree``
    GT and Model already agreed and the annotator kept that value.
``GT`` / ``Model``
    The annotator kept the ground-truth / model side of a disagreement.
``hybrid``
    Annotation-level only: fields drawn from more than one bucket.
``dropped``
    The annotator emptied a field that one side had populated.
``corrected``
    The saved value matches neither side. The app only ever clones a side, so
    a clean run leaves this at zero; a non-zero count flags either a genuine
    free-text correction or a pairing the tracer could not resolve.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from collections import Counter, defaultdict
from typing import Any, Iterable

from ner_validator_core import NERValidatorCore
from prepare_comparison_data import parse_events_json, serialize_event_for_comparison

MISSING = "MISSING"
SLOT_CATEGORIES = ["both_agree", "GT", "Model", "corrected", "dropped"]
ANN_CATEGORIES = ["both_agree", "GT", "Model", "hybrid", "corrected", "dropped"]


def load_schema(path: str) -> dict:
    import yaml

    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def schema_field_paths(schema: dict | None, banned: Iterable[str] | None = None) -> list[str]:
    """Field paths from the schema, minus any banned ones (order preserved)."""
    banned_set = set(banned or ())
    paths = [field["path"] for field in (schema or {}).get("fields", [])]
    return [path for path in paths if path not in banned_set]


def discover_annotators(annotations_dir: str, exclude: Iterable[str] | None = None) -> list[str]:
    excluded = set(exclude or ())
    names = []
    for path in sorted(glob.glob(os.path.join(annotations_dir, "*_out.json"))):
        name = os.path.basename(path)[: -len("_out.json")]
        if name not in excluded:
            names.append(name)
    return names


def _get_nested(obj: Any, path: str) -> Any:
    current = obj
    for part in path.split("->"):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def field_value(event: dict | None, path: str) -> Any:
    """Comparable value for a field: span text, scalar, or ``MISSING``."""
    if not event:
        return MISSING
    value = _get_nested(event, path)
    if value is None:
        return MISSING
    if isinstance(value, dict):
        text = value.get("text")
        return text if text not in (None, "") else MISSING
    return value


def _field_key(event: dict | None, path: str) -> Any:
    """Matching key for a field: (text, begin, end) for spans, scalar otherwise."""
    if not event:
        return None
    value = _get_nested(event, path)
    if value is None:
        return None
    if isinstance(value, dict):
        begin = value.get("begin") if value.get("begin") is not None else value.get("start")
        return (value.get("text"), begin, value.get("end"))
    return value


def _event_start(event: dict | None, fields: list[str]) -> int | None:
    starts = []
    for path in fields:
        value = _get_nested(event, path) if event else None
        if isinstance(value, dict):
            begin = value.get("begin") if value.get("begin") is not None else value.get("start")
            if begin is not None:
                starts.append(begin)
    return min(starts) if starts else None


def bucket_field(av: Any, gv: Any, mv: Any) -> str | None:
    """Classify one field of one annotation. ``None`` means "nothing here"."""
    if av == MISSING and gv == MISSING and mv == MISSING:
        return None
    if av == gv and av == mv:
        return "both_agree"
    if av == gv and av != MISSING:
        return "GT"
    if av == mv and av != MISSING:
        return "Model"
    if av == MISSING:
        return "dropped"
    return "corrected"


def pair_document(
    gt_events: list[dict], model_events: list[dict], schema: dict | None
) -> tuple[list[tuple[dict, dict]], set[str], set[str]]:
    """Reproduce the app's pairing for a single document.

    Returns ``(discrepant_pairs, perfect_sigs, realonly_sigs)``: perfect GT==Model
    auto-matches (count as both_agree) and GT-only events the model missed
    (auto-kept as GT). Both signature sets let callers pull those auto-kept
    events out of the annotator output before matching the remainder to pairs.
    """
    doc = "__doc__"
    core = NERValidatorCore(
        {doc: {"text": "", "real_events": gt_events, "model_events": model_events}}, schema
    )
    pairs = core.current_differences.get(doc, {}).get("pairs", [])
    real_sigs = {serialize_event_for_comparison(e, schema) for e in gt_events}
    model_sigs = {serialize_event_for_comparison(e, schema) for e in model_events}
    perfect = real_sigs & model_sigs
    perfect_sigs: set[str] = set()
    realonly_sigs: set[str] = set()
    for event in core.doc_final_events.get(doc, []):
        sig = serialize_event_for_comparison(event, schema)
        (perfect_sigs if sig in perfect else realonly_sigs).add(sig)
    return pairs, perfect_sigs, realonly_sigs


def _pair_score(
    ae: dict, gt_ev: dict | None, model_ev: dict | None, fields: list[str]
) -> tuple[int, int] | None:
    """Score an annotation against a pair: more exact field matches wins, then
    proximity. ``None`` if it shares nothing with either side of the pair."""
    matches = 0
    for path in fields:
        key = _field_key(ae, path)
        if key is None:
            continue
        if key == _field_key(gt_ev, path) or key == _field_key(model_ev, path):
            matches += 1
    if matches == 0:
        return None
    a_start = _event_start(ae, fields)
    penalty = None
    for side in (gt_ev, model_ev):
        side_start = _event_start(side, fields)
        if a_start is not None and side_start is not None:
            distance = abs(a_start - side_start)
            penalty = distance if penalty is None else min(penalty, distance)
    return (-matches, penalty if penalty is not None else 10**9)


def assign_events_to_pairs(
    events: list[dict], pairs: list[tuple[dict | None, dict | None]], fields: list[str]
) -> list[dict | None]:
    """Greedy one-to-one assignment of annotations to pairs (best score first)."""
    scored: list[tuple[tuple[int, int], int, int]] = []
    for pair_idx, (gt_ev, model_ev) in enumerate(pairs):
        for event_idx, ae in enumerate(events):
            score = _pair_score(ae, gt_ev, model_ev, fields)
            if score is not None:
                scored.append((score, pair_idx, event_idx))
    scored.sort()
    assigned: list[dict | None] = [None] * len(pairs)
    used_pairs: set[int] = set()
    used_events: set[int] = set()
    for _, pair_idx, event_idx in scored:
        if pair_idx in used_pairs or event_idx in used_events:
            continue
        assigned[pair_idx] = events[event_idx]
        used_pairs.add(pair_idx)
        used_events.add(event_idx)
    return assigned


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def compute_metrics(
    annotations_dir: str,
    gt_dir: str,
    model_dir: str,
    schema: dict | None,
    exclude_annotators: Iterable[str] | None = None,
    banned_fields: Iterable[str] | None = None,
) -> dict:
    """Bucket every annotation across annotators. Returns a plain-dict report."""
    fields = schema_field_paths(schema, banned_fields)
    annotators = discover_annotators(annotations_dir, exclude_annotators)
    outputs = {a: _load_json(os.path.join(annotations_dir, f"{a}_out.json")) for a in annotators}

    all_docs: set[str] = set()
    for docs in outputs.values():
        all_docs.update(docs.keys())

    pairs_by_doc: dict[str, list[tuple[dict | None, dict | None]]] = {}
    perfect_by_doc: dict[str, set[str]] = {}
    realonly_by_doc: dict[str, set[str]] = {}
    for doc in all_docs:
        gt_path = os.path.join(gt_dir, doc)
        model_path = os.path.join(model_dir, doc)
        if not (os.path.exists(gt_path) and os.path.exists(model_path)):
            continue
        gt_events = parse_events_json(gt_path)
        model_events = parse_events_json(model_path)
        pairs, perfect, realonly = pair_document(gt_events, model_events, schema)
        pairs_by_doc[doc] = pairs
        perfect_by_doc[doc] = perfect
        realonly_by_doc[doc] = realonly

    per_annotator: dict[str, dict] = {}
    slot_totals: Counter = Counter()
    ann_totals: Counter = Counter()
    per_field: dict[str, Counter] = {path: Counter() for path in fields}

    for annotator in annotators:
        slot_counts: Counter = Counter()
        ann_counts: Counter = Counter()
        pairs_seen = 0
        for doc, events in outputs[annotator].items():
            pairs = pairs_by_doc.get(doc)
            if pairs is None:
                continue
            pairs_seen += len(pairs)
            perfect = perfect_by_doc.get(doc, set())
            realonly = realonly_by_doc.get(doc, set())

            to_match: list[dict] = []
            for event in events:
                sig = serialize_event_for_comparison(event, schema)
                auto = "both_agree" if sig in perfect else ("GT" if sig in realonly else None)
                if auto is None:
                    to_match.append(event)
                    continue
                counted = False
                for path in fields:
                    if field_value(event, path) != MISSING:
                        slot_counts[auto] += 1
                        per_field[path][auto] += 1
                        counted = True
                if counted:
                    ann_counts[auto] += 1

            assigned = assign_events_to_pairs(to_match, pairs, fields)
            for pair_idx, (gt_ev, model_ev) in enumerate(pairs):
                ae = assigned[pair_idx]
                if ae is None:
                    continue
                categories: set[str] = set()
                for path in fields:
                    category = bucket_field(
                        field_value(ae, path),
                        field_value(gt_ev, path),
                        field_value(model_ev, path),
                    )
                    if category is None:
                        continue
                    slot_counts[category] += 1
                    per_field[path][category] += 1
                    categories.add(category)
                if categories:
                    ann_counts["hybrid" if len(categories) > 1 else next(iter(categories))] += 1

        per_annotator[annotator] = {
            "docs": len(outputs[annotator]),
            "annotations": sum(len(evs) for evs in outputs[annotator].values()),
            "pairs": pairs_seen,
            "slots": dict(slot_counts),
            "buckets": dict(ann_counts),
        }
        slot_totals.update(slot_counts)
        ann_totals.update(ann_counts)

    return {
        "fields": fields,
        "annotators": annotators,
        "per_annotator": per_annotator,
        "slot_totals": dict(slot_totals),
        "ann_totals": dict(ann_totals),
        "per_field": {path: dict(counts) for path, counts in per_field.items()},
    }


def _pct(part: int, whole: int) -> str:
    return f"{(100 * part / whole):.1f}%" if whole else "0.0%"


def render_markdown(metrics: dict, title: str = "Annotation metrics") -> str:
    fields = metrics["fields"]
    lines = [f"# {title}", "", f"Fields: {', '.join(fields)}", "", "## Who won", ""]
    slot_totals = metrics["slot_totals"]
    ann_totals = metrics["ann_totals"]
    total_slots = sum(slot_totals.values())
    total_anns = sum(ann_totals.values())
    lines.append("| Category | Slots | % | Anns | % |")
    lines.append("|---|---|---|---|---|")
    for category in ANN_CATEGORIES:
        slots = slot_totals.get(category, 0)
        anns = ann_totals.get(category, 0)
        slot_cell = "—" if category == "hybrid" else str(slots)
        slot_pct = "—" if category == "hybrid" else _pct(slots, total_slots)
        lines.append(f"| {category} | {slot_cell} | {slot_pct} | {anns} | {_pct(anns, total_anns)} |")
    lines.append("")
    lines.append("## Per field")
    lines.append("")
    lines.append("| Field | " + " | ".join(SLOT_CATEGORIES) + " |")
    lines.append("|---|" + "|".join(["---"] * len(SLOT_CATEGORIES)) + "|")
    for path in fields:
        counts = metrics["per_field"].get(path, {})
        total = sum(counts.values())
        cells = [f"{counts.get(cat, 0)} ({_pct(counts.get(cat, 0), total)})" for cat in SLOT_CATEGORIES]
        lines.append(f"| {path} | " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compute who-won annotation metrics.")
    parser.add_argument("--annotations-dir", required=True)
    parser.add_argument("--gt-dir", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--schema", required=True, help="Path to the comparison schema YAML.")
    parser.add_argument("--exclude", nargs="*", default=[], help="Annotator names to exclude.")
    parser.add_argument("--ban-field", nargs="*", default=[], help="Field paths to exclude from metrics.")
    parser.add_argument("--format", choices=["json", "markdown"], default="markdown")
    parser.add_argument("--title", default="Annotation metrics")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    schema = load_schema(args.schema)
    metrics = compute_metrics(
        args.annotations_dir,
        args.gt_dir,
        args.model_dir,
        schema,
        exclude_annotators=args.exclude,
        banned_fields=args.ban_field,
    )
    if args.format == "json":
        print(json.dumps(metrics, indent=2, ensure_ascii=False))
    else:
        print(render_markdown(metrics, args.title))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
