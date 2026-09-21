"""Metrics over annotator outputs: where each field landed and how reviewers agree.

Each saved annotation is traced back to the discrepant pair it came from by
reproducing the reviewer app's exact pairing (:class:`NERValidatorCore`):
perfect ground-truth/model matches and real-only events are auto-kept and never
shown to the annotator, model-only events are dropped, and only overlapping
discrepant pairs are presented. Reversing that with a naive quantity heuristic
mis-assigns events whenever a value repeats in a document, so this module goes
through the same core the reviewer uses.

Every field is read from the comparison schema, so nothing about the label set
is hard-coded. Fields can be excluded from the metrics ("banned") without
altering the pairing, and annotators are discovered from the ``<name>_out.json``
files in the annotations directory.

The report has four sections:

* **Per annotator** — documents, pairs, saved annotations and flags per reviewer.
* **Annotations vs GT** — how far each reviewer moved from ground truth.
* **Who won** — per matched pair, each field is bucketed as ``both_agree``,
  ``GT``, ``Model``, ``hybrid`` (annotation-level), ``dropped`` (a field the
  annotator emptied) or ``corrected`` (a value matching neither side; zero on a
  clean run because the app only clones a side).
* **Inter-annotator agreement** — Fleiss' kappa per field over the largest set
  of reviewers that share documents, reported twice: over every shared pair and
  over only the pairs that no reviewer flagged. Flagged pairs are the contested
  cases the flag mechanism is meant to isolate, so agreement is typically higher
  once they are excluded. A separate binary kappa measures agreement on the act
  of flagging itself — whether reviewers converge on which pairs to flag, any
  field — independent of the adjudicated values.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
from collections import Counter, defaultdict
from itertools import combinations
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


def load_flag_log(path: str) -> dict[tuple[str, int | None], list[list[str]]]:
    """Parse a reviewer flag log into ``{(doc_id, pair_index): [[field, ...], ...]}``.

    Handles the ``File: X | Pair: N | Reason: Flagged fields [a, b]`` lines the
    app writes and, defensively, a JSON-per-line variant.
    """
    entries: dict[tuple[str, int | None], list[list[str]]] = defaultdict(list)
    if not os.path.exists(path):
        return entries
    legacy = re.compile(r"File:\s*(\S+)\s*\|\s*Pair:\s*(\S+)\s*\|\s*Reason:\s*(.*)")
    bracket = re.compile(r"\[([^\]]+)\]")
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            if line.startswith("{"):
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                doc_id = payload.get("doc_id")
                if doc_id:
                    entries[(doc_id, payload.get("pair_index"))].append(list(payload.get("fields", [])))
                continue
            match = legacy.match(line)
            if not match:
                continue
            doc_id, pair, reason = match.groups()
            if "Flagged fields" not in reason:
                continue
            pair_index = int(pair) if pair.lstrip("-").isdigit() else None
            found = bracket.search(reason)
            fields = [item.strip() for item in found.group(1).split(",")] if found else []
            entries[(doc_id, pair_index)].append(fields)
    return entries


def _flag_log_path(annotations_dir: str, annotator: str) -> str | None:
    for suffix in ("_flag.log", "_flagged.log"):
        candidate = os.path.join(annotations_dir, f"{annotator}{suffix}")
        if os.path.exists(candidate):
            return candidate
    return None


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


def fleiss_kappa(matrix: list[list[int]]) -> float:
    """Fleiss' kappa for a category-count matrix (one row per item).

    Each row holds the number of raters that chose each category and must sum to
    the same rater count ``n``. Returns ``nan`` when it is undefined (no items,
    fewer than two raters, ragged rows, or no expected disagreement).
    """
    if not matrix or not matrix[0]:
        return float("nan")
    width = len(matrix[0])
    n = sum(matrix[0])
    if n < 2 or any(len(row) != width or sum(row) != n for row in matrix):
        return float("nan")
    items = len(matrix)
    total = items * n
    p_cat = [sum(row[j] for row in matrix) / total for j in range(len(matrix[0]))]
    agreement = [(sum(c * c for c in row) - n) / (n * (n - 1)) for row in matrix]
    p_bar = sum(agreement) / items
    p_expected = sum(p * p for p in p_cat)
    if p_expected >= 1.0:
        return float("nan")
    return (p_bar - p_expected) / (1 - p_expected)


def _largest_shared_group(doc_sets: dict[str, set[str]]) -> tuple[list[str], list[str]]:
    """Largest reviewer subset (>=2) that shares at least one document."""
    annotators = list(doc_sets)
    best_group: list[str] = []
    best_docs: set[str] = set()
    for size in range(len(annotators), 1, -1):
        for combo in combinations(annotators, size):
            shared = set.intersection(*(doc_sets[a] for a in combo))
            if shared and (len(combo), len(shared)) > (len(best_group), len(best_docs)):
                best_group = list(combo)
                best_docs = shared
        if best_group:
            break
    return best_group, sorted(best_docs)


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
    """Bucket every annotation and measure reviewer agreement. Returns a dict."""
    fields = schema_field_paths(schema, banned_fields)
    annotators = discover_annotators(annotations_dir, exclude_annotators)
    outputs = {a: _load_json(os.path.join(annotations_dir, f"{a}_out.json")) for a in annotators}
    flags = {a: load_flag_log(_flag_log_path(annotations_dir, a) or "") for a in annotators}

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
        pairs, perfect, realonly = pair_document(
            parse_events_json(gt_path), parse_events_json(model_path), schema
        )
        pairs_by_doc[doc] = pairs
        perfect_by_doc[doc] = perfect
        realonly_by_doc[doc] = realonly

    ann_event: dict[tuple[str, str, int], dict | None] = {}
    per_annotator: dict[str, dict] = {}
    slot_totals: Counter = Counter()
    ann_totals: Counter = Counter()
    per_field: dict[str, Counter] = {path: Counter() for path in fields}

    for annotator in annotators:
        slot_counts: Counter = Counter()
        ann_counts: Counter = Counter()
        pairs_seen = 0
        modified_vs_gt = 0
        dropped_gt = 0
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
                ann_event[(annotator, doc, pair_idx)] = ae
                if ae is None:
                    if gt_ev is not None:
                        dropped_gt += 1
                    continue
                categories: set[str] = set()
                differs_from_gt = False
                for path in fields:
                    av = field_value(ae, path)
                    if av != field_value(gt_ev, path):
                        differs_from_gt = True
                    category = bucket_field(av, field_value(gt_ev, path), field_value(model_ev, path))
                    if category is None:
                        continue
                    slot_counts[category] += 1
                    per_field[path][category] += 1
                    categories.add(category)
                if differs_from_gt:
                    modified_vs_gt += 1
                if categories:
                    ann_counts["hybrid" if len(categories) > 1 else next(iter(categories))] += 1

        flag_map = flags[annotator]
        per_annotator[annotator] = {
            "docs": len(outputs[annotator]),
            "docs_with_anns": sum(1 for evs in outputs[annotator].values() if evs),
            "annotations": sum(len(evs) for evs in outputs[annotator].values()),
            "pairs": pairs_seen,
            "flags": sum(len(v) for v in flag_map.values()),
            "flagged_docs": len({doc for (doc, _) in flag_map}),
            "modified_vs_gt": modified_vs_gt,
            "dropped_gt": dropped_gt,
            "slots": dict(slot_counts),
            "buckets": dict(ann_counts),
        }
        slot_totals.update(slot_counts)
        ann_totals.update(ann_counts)

    agreement = _compute_agreement(annotators, outputs, pairs_by_doc, ann_event, fields, flags)

    return {
        "fields": fields,
        "annotators": annotators,
        "per_annotator": per_annotator,
        "slot_totals": dict(slot_totals),
        "ann_totals": dict(ann_totals),
        "per_field": {path: dict(counts) for path, counts in per_field.items()},
        "agreement": agreement,
    }


def _compute_agreement(
    annotators: list[str],
    outputs: dict[str, dict[str, list[dict]]],
    pairs_by_doc: dict[str, list],
    ann_event: dict[tuple[str, str, int], dict | None],
    fields: list[str],
    flags: dict[str, dict[tuple[str, int | None], list]],
) -> dict | None:
    """Fleiss' kappa per field over the largest reviewer subset sharing docs.

    Computed twice over the same subset: ``all`` uses every shared pair, while
    ``unflagged`` keeps only pairs that no reviewer in the group flagged. A pair
    is treated as flagged if any group member flagged it (union), so the
    ``unflagged`` set isolates the cases reviewers left uncontested.
    """
    if len(annotators) < 2:
        return None
    doc_sets = {a: set(outputs[a].keys()) for a in annotators}
    group, shared = _largest_shared_group(doc_sets)
    if len(group) < 2 or not shared:
        return None

    def pair_flagged(doc: str, pair_idx: int) -> bool:
        return any(flags.get(a, {}).get((doc, pair_idx)) for a in group)

    all_items: list[tuple[str, int]] = []
    unflagged_items: list[tuple[str, int]] = []
    for doc in shared:
        pairs = pairs_by_doc.get(doc)
        if pairs is None:
            continue
        for pair_idx in range(len(pairs)):
            all_items.append((doc, pair_idx))
            if not pair_flagged(doc, pair_idx):
                unflagged_items.append((doc, pair_idx))

    def matrix(obs):
        # Every row lives in one shared category space so Fleiss' columns align.
        categories = sorted({label for row in obs for label in row})
        index = {category: i for i, category in enumerate(categories)}
        rows: list[list[int]] = []
        for row in obs:
            counts = [0] * len(categories)
            for label in row:
                counts[index[label]] += 1
            rows.append(counts)
        return rows

    def variant(items: list[tuple[str, int]]) -> dict | None:
        if not items:
            return None

        def observations(label_of):
            return [[label_of(a, doc, pi) for a in group] for (doc, pi) in items]

        per_field_kappa = {
            path: fleiss_kappa(matrix(observations(
                lambda a, doc, pi, p=path: field_value(ann_event.get((a, doc, pi)), p)
            )))
            for path in fields
        }
        # Overall: namespace each field's labels, then align every row at once.
        pooled_obs: list[list[str]] = []
        for path in fields:
            pooled_obs.extend(observations(
                lambda a, doc, pi, p=path: f"{p}:{field_value(ann_event.get((a, doc, pi)), p)}"
            ))
        return {"per_field": per_field_kappa, "overall": fleiss_kappa(matrix(pooled_obs))}

    # Agreement on the act of flagging itself: a binary flagged/unflagged label
    # per reviewer per pair, regardless of which field was flagged. This asks
    # whether reviewers converge on *which* pairs are worth flagging, separate
    # from whether they agree on the adjudicated values.
    flag_obs = [
        ["flag" if flags.get(a, {}).get((doc, pi)) else "unflagged" for a in group]
        for (doc, pi) in all_items
    ]
    flag_kappa = fleiss_kappa(matrix(flag_obs)) if flag_obs else float("nan")

    return {
        "group": group,
        "shared_docs": len(shared),
        "raters": len(group),
        "pairs_total": len(all_items),
        "pairs_unflagged": len(unflagged_items),
        "all": variant(all_items),
        "unflagged": variant(unflagged_items),
        "flag_kappa": flag_kappa,
    }


def _pct(part: int, whole: int) -> str:
    return f"{(100 * part / whole):.1f}%" if whole else "0.0%"


def _kappa_cell(value: float) -> str:
    return "N/A" if value != value else f"{value:.3f}"  # value != value catches nan


def render_markdown(metrics: dict, title: str = "Annotation metrics") -> str:
    fields = metrics["fields"]
    per_annotator = metrics["per_annotator"]
    lines = [f"# {title}", "", f"Fields: {', '.join(fields)}", ""]

    lines += ["## 1. Annotator overview", "", "### Per annotator", ""]
    lines.append("| Annotator | Docs | Docs w/ ann | Pairs | Anns | Flags | Flagged docs | % docs flagged | % pairs flagged |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for annotator in metrics["annotators"]:
        stats = per_annotator[annotator]
        lines.append(
            f"| {annotator} | {stats['docs']} | {stats['docs_with_anns']} | {stats['pairs']} | "
            f"{stats['annotations']} | {stats['flags']} | {stats['flagged_docs']} | "
            f"{_pct(stats['flagged_docs'], stats['docs'])} | {_pct(stats['flags'], stats['pairs'])} |"
        )
    lines.append("")

    lines += ["### Annotations vs GT", "", "| Annotator | Anns / doc | Modified vs GT | Dropped GT |", "|---|---|---|---|"]
    for annotator in metrics["annotators"]:
        stats = per_annotator[annotator]
        per_doc = stats["annotations"] / stats["docs_with_anns"] if stats["docs_with_anns"] else 0.0
        lines.append(
            f"| {annotator} | {per_doc:.1f} | "
            f"{stats['modified_vs_gt']} ({_pct(stats['modified_vs_gt'], stats['pairs'])}) | "
            f"{stats['dropped_gt']} ({_pct(stats['dropped_gt'], stats['pairs'])}) |"
        )
    lines.append("")

    lines += ["## 2. Who won — GT vs Model vs correction", ""]
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

    lines += ["## 3. Inter-annotator agreement (Fleiss κ)", ""]
    agreement = metrics.get("agreement")
    if not agreement:
        lines.append("_No documents shared by two or more annotators._")
        lines.append("")
        return "\n".join(lines)
    lines.append(
        f"Largest overlapping subset: {', '.join(agreement['group'])} "
        f"({agreement['raters']} reviewers, {agreement['shared_docs']} shared docs)."
    )
    flagged = agreement["pairs_total"] - agreement["pairs_unflagged"]
    lines.append(
        f"{agreement['pairs_total']} overlap pairs, {flagged} flagged by at least "
        f"one reviewer, {agreement['pairs_unflagged']} unflagged."
    )
    lines.append("")

    def _variant_cell(variant: dict | None, path: str) -> str:
        if not variant:
            return "N/A"
        return _kappa_cell(variant["per_field"].get(path, float("nan")))

    def _overall_cell(variant: dict | None) -> str:
        return _kappa_cell(variant["overall"]) if variant else "N/A"

    unflagged, all_pairs = agreement["unflagged"], agreement["all"]
    lines.append("| Field | κ (unflagged pairs) | κ (all pairs) |")
    lines.append("|---|---|---|")
    for path in fields:
        lines.append(
            f"| {path} | {_variant_cell(unflagged, path)} | {_variant_cell(all_pairs, path)} |"
        )
    lines.append(
        f"| **overall** | **{_overall_cell(unflagged)}** | **{_overall_cell(all_pairs)}** |"
    )
    lines.append("")
    lines.append(
        "Agreement on the act of flagging (binary flagged/unflagged per pair, "
        f"any field), Fleiss κ: **{_kappa_cell(agreement['flag_kappa'])}**."
    )
    lines.append("")
    return "\n".join(lines)


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compute annotation metrics and reviewer agreement.")
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
