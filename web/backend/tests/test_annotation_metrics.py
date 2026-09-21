import json
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import annotation_metrics as am

SCHEMA = {
    "fields": [
        {"path": "quantity"},
        {"path": "unit"},
        {"path": "eventType"},
    ]
}


def span(text, begin, end):
    return {"text": text, "begin": begin, "end": end}


class TmpProject:
    """A throwaway annotations/GT/model directory tree for one test."""

    def __init__(self):
        self.root = tempfile.mkdtemp()
        self.gt = os.path.join(self.root, "gt")
        self.model = os.path.join(self.root, "model")
        self.ann = os.path.join(self.root, "ann")
        for path in (self.gt, self.model, self.ann):
            os.makedirs(path)

    def add_doc(self, doc, gt_events, model_events):
        self._write(os.path.join(self.gt, doc), gt_events)
        self._write(os.path.join(self.model, doc), model_events)

    def add_output(self, annotator, mapping):
        self._write(os.path.join(self.ann, f"{annotator}_out.json"), mapping)

    def add_flag_log(self, annotator, text):
        with open(os.path.join(self.ann, f"{annotator}_flag.log"), "w", encoding="utf-8") as handle:
            handle.write(text)

    @staticmethod
    def _write(path, payload):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def metrics(self, **kwargs):
        return am.compute_metrics(self.ann, self.gt, self.model, SCHEMA, **kwargs)


class BucketFieldTests(unittest.TestCase):
    def test_all_missing_is_none(self):
        self.assertIsNone(am.bucket_field(am.MISSING, am.MISSING, am.MISSING))

    def test_both_agree(self):
        self.assertEqual(am.bucket_field("5", "5", "5"), "both_agree")

    def test_gt_and_model_picks(self):
        self.assertEqual(am.bucket_field("5", "5", "6"), "GT")
        self.assertEqual(am.bucket_field("6", "5", "6"), "Model")

    def test_dropped_when_annotator_empties(self):
        self.assertEqual(am.bucket_field(am.MISSING, "5", am.MISSING), "dropped")

    def test_corrected_when_matches_neither(self):
        self.assertEqual(am.bucket_field("7", "5", "6"), "corrected")


class SchemaFieldTests(unittest.TestCase):
    def test_paths_from_schema(self):
        self.assertEqual(am.schema_field_paths(SCHEMA), ["quantity", "unit", "eventType"])

    def test_banned_field_removed(self):
        self.assertEqual(am.schema_field_paths(SCHEMA, banned=["eventType"]), ["quantity", "unit"])

    def test_empty_schema(self):
        self.assertEqual(am.schema_field_paths(None), [])


class DiscoveryTests(unittest.TestCase):
    def test_discovers_and_excludes(self):
        project = TmpProject()
        project.add_output("alice", {})
        project.add_output("bob", {})
        self.assertEqual(am.discover_annotators(project.ann), ["alice", "bob"])
        self.assertEqual(am.discover_annotators(project.ann, exclude=["bob"]), ["alice"])


class WhoWonTests(unittest.TestCase):
    def test_perfect_match_is_both_agree_and_makes_no_pair(self):
        project = TmpProject()
        event = {"quantity": span("5", 0, 1), "unit": span("people", 2, 8), "eventType": "EventP"}
        project.add_doc("d.json", [event], [dict(event)])
        project.add_output("a", {"d.json": [dict(event)]})
        result = project.metrics()
        self.assertEqual(result["per_annotator"]["a"]["pairs"], 0)
        self.assertEqual(result["ann_totals"].get("both_agree"), 1)
        self.assertNotIn("corrected", result["slot_totals"])

    def test_gt_pick_on_discrepant_pair(self):
        project = TmpProject()
        gt = {"quantity": span("5", 0, 1), "unit": span("people", 2, 8), "eventType": "EventP"}
        model = {"quantity": span("5", 0, 1), "unit": span("persons", 2, 9), "eventType": "EventP"}
        project.add_doc("d.json", [gt], [model])
        project.add_output("a", {"d.json": [dict(gt)]})
        result = project.metrics()
        self.assertEqual(result["per_annotator"]["a"]["pairs"], 1)
        self.assertEqual(result["per_field"]["unit"].get("GT"), 1)
        self.assertEqual(result["slot_totals"].get("corrected", 0), 0)

    def test_model_pick_on_discrepant_pair(self):
        project = TmpProject()
        gt = {"quantity": span("5", 0, 1), "unit": span("people", 2, 8), "eventType": "EventP"}
        model = {"quantity": span("5", 0, 1), "unit": span("persons", 2, 9), "eventType": "EventP"}
        project.add_doc("d.json", [gt], [model])
        project.add_output("a", {"d.json": [dict(model)]})
        result = project.metrics()
        self.assertEqual(result["per_field"]["unit"].get("Model"), 1)

    def test_hybrid_annotation(self):
        project = TmpProject()
        gt = {"quantity": span("5", 0, 1), "unit": span("people", 2, 8), "eventType": "EventP"}
        model = {"quantity": span("5", 0, 1), "unit": span("persons", 2, 9), "eventType": "EventO"}
        project.add_doc("d.json", [gt], [model])
        # Keep GT unit but Model eventType -> a field-by-field hybrid.
        saved = {"quantity": span("5", 0, 1), "unit": span("people", 2, 8), "eventType": "EventO"}
        project.add_output("a", {"d.json": [saved]})
        result = project.metrics()
        self.assertEqual(result["ann_totals"].get("hybrid"), 1)
        self.assertEqual(result["per_field"]["unit"].get("GT"), 1)
        self.assertEqual(result["per_field"]["eventType"].get("Model"), 1)
        self.assertEqual(result["slot_totals"].get("corrected", 0), 0)

    def test_dropped_field(self):
        project = TmpProject()
        gt = {"quantity": span("5", 0, 1), "unit": span("people", 2, 8), "eventType": "EventP"}
        model = {"quantity": span("5", 0, 1), "eventType": "EventP"}
        project.add_doc("d.json", [gt], [model])
        # Annotator keeps the pair but leaves the unit empty.
        saved = {"quantity": span("5", 0, 1), "eventType": "EventP"}
        project.add_output("a", {"d.json": [saved]})
        result = project.metrics()
        self.assertEqual(result["per_field"]["unit"].get("dropped"), 1)

    def test_cloning_a_side_never_produces_corrected(self):
        """Repeated quantities must not fool the tracer into a false correction."""
        project = TmpProject()
        gt = [
            {"quantity": span("5", 0, 1), "unit": span("people", 2, 8), "eventType": "EventP"},
            {"quantity": span("5", 50, 51), "unit": span("cases", 52, 57), "eventType": "EventO"},
        ]
        model = [
            {"quantity": span("5", 0, 1), "unit": span("persons", 2, 9), "eventType": "EventP"},
            {"quantity": span("5", 50, 51), "unit": span("incidents", 52, 61), "eventType": "EventO"},
        ]
        project.add_doc("d.json", gt, model)
        # Annotator kept GT on the first pair, Model on the second.
        project.add_output("a", {"d.json": [dict(gt[0]), dict(model[1])]})
        result = project.metrics()
        self.assertEqual(result["slot_totals"].get("corrected", 0), 0)
        self.assertEqual(result["per_field"]["unit"].get("GT"), 1)
        self.assertEqual(result["per_field"]["unit"].get("Model"), 1)

    def test_free_text_correction_is_flagged(self):
        project = TmpProject()
        gt = {"quantity": span("5", 0, 1), "unit": span("people", 2, 8), "eventType": "EventP"}
        model = {"quantity": span("5", 0, 1), "unit": span("people", 2, 8), "eventType": "EventO"}
        project.add_doc("d.json", [gt], [model])
        # Annotator overrode eventType to a value on neither side.
        saved = {"quantity": span("5", 0, 1), "unit": span("people", 2, 8), "eventType": "EventA"}
        project.add_output("a", {"d.json": [saved]})
        result = project.metrics()
        self.assertEqual(result["per_field"]["eventType"].get("corrected"), 1)


class BanAndExcludeTests(unittest.TestCase):
    def _project_with_eventtype_discrepancy(self):
        project = TmpProject()
        gt = {"quantity": span("5", 0, 1), "unit": span("people", 2, 8), "eventType": "EventP"}
        model = {"quantity": span("5", 0, 1), "unit": span("persons", 2, 9), "eventType": "EventO"}
        project.add_doc("d.json", [gt], [model])
        project.add_output("a", {"d.json": [dict(gt)]})
        project.add_output("b", {"d.json": [dict(model)]})
        return project

    def test_banned_field_absent_from_metrics(self):
        project = self._project_with_eventtype_discrepancy()
        result = project.metrics(banned_fields=["eventType"])
        self.assertEqual(result["fields"], ["quantity", "unit"])
        self.assertNotIn("eventType", result["per_field"])

    def test_excluded_annotator_absent(self):
        project = self._project_with_eventtype_discrepancy()
        result = project.metrics(exclude_annotators=["b"])
        self.assertEqual(result["annotators"], ["a"])
        self.assertNotIn("b", result["per_annotator"])


class FleissKappaTests(unittest.TestCase):
    def test_full_agreement_split_items_is_one(self):
        # Two items, three raters, everyone agrees within each item, items differ.
        self.assertAlmostEqual(am.fleiss_kappa([[3, 0], [0, 3]]), 1.0)

    def test_partial_disagreement_is_below_one(self):
        self.assertAlmostEqual(am.fleiss_kappa([[2, 1], [1, 2]]), -1 / 3, places=6)

    def test_ragged_rows_are_undefined(self):
        self.assertTrue(math.isnan(am.fleiss_kappa([[3, 0], [2, 0]])))

    def test_single_rater_is_undefined(self):
        self.assertTrue(math.isnan(am.fleiss_kappa([[1, 0], [1, 0]])))

    def test_empty_is_undefined(self):
        self.assertTrue(math.isnan(am.fleiss_kappa([])))


class FlagLogTests(unittest.TestCase):
    def test_parses_legacy_lines_and_ignores_comments(self):
        project = TmpProject()
        project.add_flag_log(
            "a",
            "File: d.json | Pair: 0 | Reason: Flagged fields [unit]\n"
            "# unit should be missing\n"
            "File: d.json | Pair: 2 | Reason: Flagged fields [eventType, unit]\n",
        )
        flags = am.load_flag_log(os.path.join(project.ann, "a_flag.log"))
        self.assertEqual(flags[("d.json", 0)], [["unit"]])
        self.assertEqual(flags[("d.json", 2)], [["eventType", "unit"]])

    def test_skip_lines_are_not_flags(self):
        project = TmpProject()
        project.add_flag_log("a", "File: d.json | Pair: 0 | Reason: Skipped (no side selected, no flag)\n")
        self.assertEqual(len(am.load_flag_log(os.path.join(project.ann, "a_flag.log"))), 0)


class OverviewTests(unittest.TestCase):
    def test_flags_and_vs_gt_counts(self):
        project = TmpProject()
        gt = {"quantity": span("5", 0, 1), "unit": span("people", 2, 8), "eventType": "EventP"}
        model = {"quantity": span("5", 0, 1), "unit": span("persons", 2, 9), "eventType": "EventP"}
        project.add_doc("d.json", [gt], [model])
        # Annotator kept the Model unit (so the pair is modified vs GT) and flagged it.
        project.add_output("a", {"d.json": [dict(model)]})
        project.add_flag_log("a", "File: d.json | Pair: 0 | Reason: Flagged fields [unit]\n")
        stats = project.metrics()["per_annotator"]["a"]
        self.assertEqual(stats["flags"], 1)
        self.assertEqual(stats["flagged_docs"], 1)
        self.assertEqual(stats["modified_vs_gt"], 1)
        self.assertEqual(stats["pairs"], 1)

    def test_dropped_gt_when_pair_left_undecided(self):
        project = TmpProject()
        gt = {"quantity": span("5", 0, 1), "unit": span("people", 2, 8), "eventType": "EventP"}
        model = {"quantity": span("5", 0, 1), "unit": span("persons", 2, 9), "eventType": "EventP"}
        project.add_doc("d.json", [gt], [model])
        # The reviewer saved nothing for the pair -> the GT event was dropped.
        project.add_output("a", {"d.json": []})
        stats = project.metrics()["per_annotator"]["a"]
        self.assertEqual(stats["dropped_gt"], 1)


class AgreementTests(unittest.TestCase):
    def _shared_doc_project(self, a_unit, b_unit):
        project = TmpProject()
        gt = {"quantity": span("5", 0, 1), "unit": span("people", 2, 8), "eventType": "EventP"}
        model = {"quantity": span("5", 0, 1), "unit": span("persons", 2, 9), "eventType": "EventO"}
        gt2 = {"quantity": span("9", 20, 21), "unit": span("cases", 22, 27), "eventType": "EventO"}
        model2 = {"quantity": span("9", 20, 21), "unit": span("incidents", 22, 31), "eventType": "EventP"}
        project.add_doc("d.json", [gt, gt2], [model, model2])

        def saved(unit_choice):
            first = dict(gt) if unit_choice == "gt" else dict(model)
            return [first, dict(gt2)]

        project.add_output("a", {"d.json": saved(a_unit)})
        project.add_output("b", {"d.json": saved(b_unit)})
        return project

    def test_agreement_present_for_shared_docs(self):
        result = self._shared_doc_project("gt", "gt").metrics()
        agreement = result["agreement"]
        self.assertIsNotNone(agreement)
        self.assertEqual(sorted(agreement["group"]), ["a", "b"])
        self.assertEqual(agreement["shared_docs"], 1)
        self.assertIn("eventType", agreement["all"]["per_field"])

    def test_uneven_category_spread_does_not_crash(self):
        # Reviewers disagree on one pair and agree on another, so the two items
        # span different numbers of categories; Fleiss must align them, not throw.
        result = self._shared_doc_project("gt", "model").metrics()
        agreement = result["agreement"]
        self.assertIsNotNone(agreement)
        for value in agreement["all"]["per_field"].values():
            self.assertTrue(isinstance(value, float))
        self.assertTrue(isinstance(agreement["all"]["overall"], float))

    def test_flag_split_partitions_pairs(self):
        # One of the two shared pairs is flagged -> the unflagged variant keeps
        # the other, and both variants are still computed.
        project = self._shared_doc_project("gt", "gt")
        project.add_flag_log("a", "File: d.json | Pair: 0 | Reason: Flagged fields [unit]\n")
        agreement = project.metrics()["agreement"]
        self.assertEqual(agreement["pairs_total"], 2)
        self.assertEqual(agreement["pairs_unflagged"], 1)
        self.assertIsNotNone(agreement["all"])
        self.assertIsNotNone(agreement["unflagged"])
        self.assertIn("eventType", agreement["unflagged"]["per_field"])

    def test_no_flags_makes_variants_identical(self):
        agreement = self._shared_doc_project("gt", "model").metrics()["agreement"]
        self.assertEqual(agreement["pairs_unflagged"], agreement["pairs_total"])
        self.assertEqual(agreement["unflagged"]["overall"], agreement["all"]["overall"])

    def test_all_pairs_flagged_drops_unflagged_variant(self):
        # A flag from either reviewer marks the pair as contested (union rule).
        project = self._shared_doc_project("gt", "gt")
        project.add_flag_log("a", "File: d.json | Pair: 0 | Reason: Flagged fields [unit]\n")
        project.add_flag_log("b", "File: d.json | Pair: 1 | Reason: Flagged fields [unit]\n")
        agreement = project.metrics()["agreement"]
        self.assertEqual(agreement["pairs_unflagged"], 0)
        self.assertIsNone(agreement["unflagged"])
        self.assertIsNotNone(agreement["all"])

    def test_no_agreement_with_single_annotator(self):
        project = TmpProject()
        gt = {"quantity": span("5", 0, 1), "unit": span("people", 2, 8), "eventType": "EventP"}
        model = {"quantity": span("5", 0, 1), "unit": span("persons", 2, 9), "eventType": "EventP"}
        project.add_doc("d.json", [gt], [model])
        project.add_output("solo", {"d.json": [dict(gt)]})
        self.assertIsNone(project.metrics()["agreement"])


class RenderTests(unittest.TestCase):
    def test_markdown_renders_all_categories(self):
        project = TmpProject()
        gt = {"quantity": span("5", 0, 1), "unit": span("people", 2, 8), "eventType": "EventP"}
        model = {"quantity": span("5", 0, 1), "unit": span("persons", 2, 9), "eventType": "EventP"}
        project.add_doc("d.json", [gt], [model])
        project.add_output("a", {"d.json": [dict(gt)]})
        text = am.render_markdown(project.metrics(), title="T")
        self.assertIn("# T", text)
        for heading in ("### Per annotator", "### Annotations vs GT",
                        "## 2. Who won", "Fleiss"):
            self.assertIn(heading, text)
        for category in am.ANN_CATEGORIES:
            self.assertIn(category, text)

    def test_agreement_table_renders_both_flag_columns(self):
        project = TmpProject()
        gt = {"quantity": span("5", 0, 1), "unit": span("people", 2, 8), "eventType": "EventP"}
        model = {"quantity": span("5", 0, 1), "unit": span("persons", 2, 9), "eventType": "EventO"}
        gt2 = {"quantity": span("9", 20, 21), "unit": span("cases", 22, 27), "eventType": "EventO"}
        model2 = {"quantity": span("9", 20, 21), "unit": span("incidents", 22, 31), "eventType": "EventP"}
        project.add_doc("d.json", [gt, gt2], [model, model2])
        project.add_output("a", {"d.json": [dict(gt), dict(gt2)]})
        project.add_output("b", {"d.json": [dict(gt), dict(gt2)]})
        project.add_flag_log("a", "File: d.json | Pair: 0 | Reason: Flagged fields [unit]\n")
        text = am.render_markdown(project.metrics(), title="T")
        self.assertIn("κ (unflagged pairs)", text)
        self.assertIn("κ (all pairs)", text)
        self.assertIn("flagged by at least", text)


if __name__ == "__main__":
    unittest.main()
