"""Generate the synthetic sample corpus shipped with the demo package.

The real corpus used in our experiments is third-party data that we cannot
redistribute. To keep the tool runnable end-to-end by anyone who downloads it,
this script builds a small fabricated corpus in the same shape: source texts
plus ground-truth and model annotations with byte offsets that really index
into the text.

Spans are never written by hand. Each sentence is authored with inline markup
(``{q1:673}`` marks the quantity of event 1, ``{u1:measles cases}`` its unit,
and so on) and the offsets are computed while the text is assembled, so a span
can never drift out of sync with the text it points at. The event number is
explicit rather than inferred from position, because real sentences put the
description before the quantity often enough that any positional rule is wrong.

    python scripts/make_sample_data.py

Everything is deterministic: the same input always produces the same corpus,
which matters because the demo, the screencast and the tests all read it.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Inline markup: {<field-code><event-number>:<surface text>}
FIELD_CODES = {
    "q": "quantity",
    "u": "unit",
    "m": "modifier",
    "d": "eventDescription",
}
MARKUP_RE = re.compile(r"\{([qumd])(\d+):([^}]*)\}")

# EventP = the event is presented as having happened; EventO = anything else
# (negated, hypothetical, forecast). Kept deliberately coarse: the point of the
# sample corpus is to exercise the comparison UI, not to model epidemiology.
EVENT_P = "EventP"
EVENT_O = "EventO"


def render(markup: str) -> tuple[str, list[dict]]:
    """Turn marked-up text into plain text plus the spans it declared.

    Returns the rendered text and one dict per ``{code:text}`` group, each
    carrying the field name and the offsets of the surface text *in the
    rendered output* (not in the markup).
    """
    out: list[str] = []
    spans: list[dict] = []
    cursor = 0
    pos = 0

    for match in MARKUP_RE.finditer(markup):
        literal = markup[cursor:match.start()]
        out.append(literal)
        pos += len(literal)

        code, event_no, surface = match.group(1), int(match.group(2)), match.group(3)
        spans.append({
            "event": event_no,
            "field": FIELD_CODES[code],
            "text": surface,
            "begin": pos,
            "end": pos + len(surface),
        })
        out.append(surface)
        pos += len(surface)
        cursor = match.end()

    out.append(markup[cursor:])
    return "".join(out), spans


def group_events(spans: list[dict], event_types: list[str]) -> list[dict]:
    """Fold a flat span list into events, keyed by the event number in the markup.

    Events come out ordered by that number, so the ground truth reads in the
    same order it was authored regardless of where each field sits in the text.
    """
    by_event: dict[int, dict] = {}

    for span in spans:
        event = by_event.setdefault(span["event"], {})
        if span["field"] in event:
            raise ValueError(
                f"event {span['event']} declares {span['field']} twice "
                f"({event[span['field']]['text']!r} and {span['text']!r}); "
                "use a separate event number"
            )
        event[span["field"]] = {
            "text": span["text"],
            "begin": span["begin"],
            "end": span["end"],
        }

    events = [by_event[k] for k in sorted(by_event)]

    if len(events) != len(event_types):
        raise ValueError(
            f"markup declares {len(events)} event(s) but {len(event_types)} "
            "event type(s) were given"
        )
    for event, event_type in zip(events, event_types):
        event["eventType"] = event_type

    return events


# --- the corpus -------------------------------------------------------------
#
# Each document declares the marked-up source text, the event types for the
# ground truth, and how the model output differs from it. The `model` entry is a
# list of mutations applied to a copy of the ground truth, so every document
# states its disagreement explicitly instead of duplicating annotations.
#
# Mutations:
#   ("drop", i)                     model missed event i
#   ("add_at", surface, type)       model invented an event over `surface`
#   ("retype", i, type)             model assigned a different eventType
#   ("shift", i, field, d_begin, d_end)   model's span boundaries are off
#   ("retext", i, field, text)      model read a different surface string
#   ("remove", i, field)            model missed one field of an event

DOCUMENTS = [
    {
        "id": "sample_001",
        "text": (
            "Regional health authorities said that {q1:673} {u1:measles cases} were "
            "{d1:reported} across the northern districts during the last quarter. "
            "A further {q2:41} {u2:deaths} were {m2:provisionally} {d2:attributed} "
            "to the outbreak."
        ),
        "gt_types": [EVENT_P, EVENT_P],
        # The model clipped the unit to the head noun and missed the hedge.
        "model": [("retext", 0, "unit", "cases"), ("remove", 1, "modifier")],
    },
    {
        "id": "sample_002",
        "text": (
            "The agency estimates that {q1:12,000} {u1:households} were "
            "{d1:displaced} by flooding in the coastal lowlands. Officials warned "
            "that up to {q2:3,500} more {u2:families} could be {m2:potentially} "
            "{d2:affected} if the river continues to rise."
        ),
        # The second event is a forecast, not a report.
        "gt_types": [EVENT_P, EVENT_O],
        # Classic failure: the model reads a hedged forecast as a fact.
        "model": [("retype", 1, EVENT_P)],
    },
    {
        "id": "sample_003",
        "text": (
            "A cholera outbreak in the eastern province has left {q1:218} people "
            "{d1:hospitalised}. Local clinics reported {q2:19} {u2:fatalities} "
            "over the same period."
        ),
        "gt_types": [EVENT_P, EVENT_P],
        # The model missed the second sentence entirely.
        "model": [("drop", 1)],
    },
    {
        "id": "sample_004",
        "text": (
            "Field teams vaccinated {q1:45,800} {u1:children} under five against "
            "polio during the campaign. No adverse events were recorded."
        ),
        "gt_types": [EVENT_P],
        # The model hallucinated an event out of the negated sentence: there
        # were no adverse events, but it extracted one anyway.
        "model": [("add_at", "adverse events", EVENT_P)],
    },
    {
        "id": "sample_005",
        "text": (
            "According to the ministry, {q1:1,204} {u1:dengue cases} were "
            "{d1:confirmed} in the capital last month, a figure {m1:roughly} "
            "double the seasonal average."
        ),
        "gt_types": [EVENT_P],
        # Off-by-a-few boundary: the kind of near-miss the UI has to surface.
        "model": [("shift", 0, "unit", 0, -6)],
    },
    {
        "id": "sample_006",
        "text": (
            "Aid organisations distributed {q1:8,900} {u1:shelter kits} to "
            "families in the affected area. Another {q2:2,100} {u2:kits} remain "
            "in storage and have {m2:not} yet been {d2:delivered}."
        ),
        "gt_types": [EVENT_P, EVENT_O],
        # Negation dropped, so a pending delivery reads as a completed one.
        "model": [("retype", 1, EVENT_P), ("remove", 1, "modifier")],
    },
    {
        "id": "sample_007",
        "text": (
            "Surveillance data show {q1:56} {u1:suspected cases} of viral "
            "haemorrhagic fever. Of these, {q2:7} have been "
            "{d2:laboratory-confirmed}."
        ),
        "gt_types": [EVENT_O, EVENT_P],
        # Ground truth and model agree completely. This document should be
        # filtered out before it ever reaches an annotator, which is itself
        # worth demonstrating.
        "model": [],
    },
    {
        "id": "sample_008",
        "text": (
            "The earthquake damaged {q1:3,412} {u1:homes} and left {q2:15,600} "
            "people {d2:without shelter}. Assessment teams are still "
            "{m2:unable} to reach two mountain villages."
        ),
        "gt_types": [EVENT_P, EVENT_P],
        # The model cut the description short.
        "model": [("retext", 1, "eventDescription", "without")],
    },
    {
        "id": "sample_009",
        # The description precedes the quantity here, which is exactly the case
        # a positional grouping rule would get wrong.
        "text": (
            "Authorities {d1:confirmed} {q1:92} {u1:new infections} in the "
            "detention facility. A spokesperson said {q2:340} {u2:tests} had "
            "been processed."
        ),
        "gt_types": [EVENT_P, EVENT_P],
        # Both events retyped: a document where the model is systematically off.
        "model": [("retype", 0, EVENT_O), ("retype", 1, EVENT_O)],
    },
    {
        "id": "sample_010",
        "text": (
            "Nutrition screening found {q1:1,870} {u1:children} with acute "
            "malnutrition. The programme aims to reach {q2:5,000} {u2:children} "
            "by the end of the year, though funding {m2:may} fall short."
        ),
        "gt_types": [EVENT_P, EVENT_O],
        # The model missed the target figure and swallowed the unit into the
        # quantity span.
        "model": [("drop", 1), ("shift", 0, "quantity", 0, 9)],
    },
    {
        "id": "sample_011",
        "text": (
            "A measles vaccination drive reached {q1:22,450} {u1:children} across "
            "eleven districts. Health workers {d2:identified} {q2:63} "
            "{u2:zero-dose children} during the same visits."
        ),
        "gt_types": [EVENT_P, EVENT_P],
        "model": [("retext", 1, "unit", "children")],
    },
    {
        "id": "sample_012",
        "text": (
            "Flooding {d1:destroyed} {q1:640} {u1:hectares} of farmland. The "
            "ministry has {m2:not} yet {d2:verified} reports of {q2:120} "
            "additional {u2:hectares} downstream."
        ),
        "gt_types": [EVENT_P, EVENT_O],
        "model": [("retype", 1, EVENT_P), ("drop", 0)],
    },
]


def nearest_occurrence(text: str, surface: str, anchor: int) -> int:
    """Offset of the occurrence of `surface` closest to `anchor`."""
    starts = []
    pos = text.find(surface)
    while pos >= 0:
        starts.append(pos)
        pos = text.find(surface, pos + 1)
    if not starts:
        raise ValueError(f"{surface!r} does not occur in the source text")
    return min(starts, key=lambda s: abs(s - anchor))


def apply_mutations(events: list[dict], mutations: list, text: str) -> list[dict]:
    """Return a copy of `events` with the model's divergences applied."""
    model = json.loads(json.dumps(events))
    dropped: set[int] = set()
    added: list[dict] = []

    for mutation in mutations:
        op = mutation[0]
        if op == "drop":
            dropped.add(mutation[1])
        elif op == "add_at":
            _, surface, event_type = mutation
            begin = text.find(surface)
            if begin < 0:
                raise ValueError(f"{surface!r} does not occur in the source text")
            added.append({
                "quantity": {
                    "text": surface,
                    "begin": begin,
                    "end": begin + len(surface),
                },
                "eventType": event_type,
            })
        elif op == "retype":
            model[mutation[1]]["eventType"] = mutation[2]
        elif op == "remove":
            model[mutation[1]].pop(mutation[2], None)
        elif op == "retext":
            # Re-locate the replacement in the source rather than trimming the
            # existing offsets: keeping `begin` and shortening `end` would cut
            # mid-word ("measles cases" -> "measl") instead of selecting the
            # substring we asked for.
            _, idx, field, surface = mutation
            span = model[idx].get(field)
            if span:
                begin = nearest_occurrence(text, surface, span["begin"])
                span["begin"], span["end"] = begin, begin + len(surface)
                span["text"] = surface
        elif op == "shift":
            _, idx, field, d_begin, d_end = mutation
            span = model[idx].get(field)
            if span:
                span["begin"] += d_begin
                span["end"] += d_end
        else:
            raise ValueError(f"unknown mutation: {op}")

    kept = [e for i, e in enumerate(model) if i not in dropped]
    return kept + added


def realign(events: list[dict], text: str) -> None:
    """Make every span's surface text match what the offsets actually cover.

    Mutations move offsets around; this keeps `text` honest so the frontend
    highlights exactly what the annotation claims. Ground-truth spans are
    already consistent by construction, so this only ever rewrites model spans.
    """
    for event in events:
        for field in FIELD_CODES.values():
            span = event.get(field)
            if not isinstance(span, dict):
                continue
            begin = max(0, min(span["begin"], len(text)))
            end = max(begin, min(span["end"], len(text)))
            span["begin"], span["end"] = begin, end
            span["text"] = text[begin:end]


def build(out_root: Path) -> dict:
    source_dir = out_root / "source"
    gt_dir = out_root / "ground_truth"
    model_dir = out_root / "model_tags"
    for d in (source_dir, gt_dir, model_dir):
        d.mkdir(parents=True, exist_ok=True)

    identical = 0
    for doc in DOCUMENTS:
        text, spans = render(doc["text"])
        gt_events = group_events(spans, doc["gt_types"])
        model_events = apply_mutations(gt_events, doc["model"], text)
        realign(model_events, text)

        (source_dir / f"{doc['id']}.txt").write_text(text, encoding="utf-8")
        for directory, events in ((gt_dir, gt_events), (model_dir, model_events)):
            path = directory / f"{doc['id']}.json"
            path.write_text(
                json.dumps(events, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

        if gt_events == model_events:
            identical += 1

    return {
        "documents": len(DOCUMENTS),
        "identical": identical,
        "discrepant": len(DOCUMENTS) - identical,
    }


def verify(out_root: Path) -> list[str]:
    """Check every span against the text it points at.

    Two things are checked. First, that the offsets really cover the surface
    string the span declares. Second, that no span starts or ends inside a word:
    a model that clips "measles cases" to "measl" reads as a corpus bug rather
    than as the boundary error we meant to demonstrate, and the whole point of
    this corpus is to be shown to people.
    """
    problems: list[str] = []
    for kind in ("ground_truth", "model_tags"):
        for path in sorted((out_root / kind).glob("*.json")):
            text = (out_root / "source" / f"{path.stem}.txt").read_text(encoding="utf-8")
            for i, event in enumerate(json.loads(path.read_text(encoding="utf-8"))):
                for field, span in event.items():
                    if not isinstance(span, dict):
                        continue
                    begin, end = span["begin"], span["end"]
                    actual = text[begin:end]
                    where = f"{kind}/{path.name} event {i} {field}"

                    if actual != span["text"]:
                        problems.append(
                            f"{where}: offsets cover {actual!r} "
                            f"but span says {span['text']!r}"
                        )
                        continue

                    starts_mid = begin > 0 and text[begin - 1].isalnum() and text[begin].isalnum()
                    ends_mid = end < len(text) and text[end].isalnum() and text[end - 1].isalnum()
                    if starts_mid or ends_mid:
                        problems.append(
                            f"{where}: {actual!r} cuts a word "
                            f"(context: {text[max(0, begin - 12):end + 12]!r})"
                        )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=PROJECT_ROOT / "data" / "sample",
        help="where to write the corpus (default: data/sample)",
    )
    args = parser.parse_args()

    stats = build(args.out)
    problems = verify(args.out)

    rel = args.out.relative_to(PROJECT_ROOT) if args.out.is_relative_to(PROJECT_ROOT) else args.out
    print(f"Wrote {stats['documents']} documents to {rel}/")
    print(f"  {stats['discrepant']} with a ground-truth/model discrepancy")
    print(f"  {stats['identical']} identical (filtered out before review)")

    if problems:
        print(f"\n{len(problems)} span(s) do not match their offsets:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("\nAll spans verified against their source text.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
