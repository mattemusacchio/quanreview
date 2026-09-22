import json
import os
import sys
import tempfile
import unittest

import yaml


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, ROOT_DIR)


from scripts.integrate_annotations import integrate_project
from scripts.resolve_conflicts import resolve_conflict


class TestAnnotationIntegration(unittest.TestCase):
    def test_integrate_project_computes_iaa_and_auto_merges_unanimous_docs(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_dir = os.path.join(tmp_dir, "project")
            os.makedirs(project_dir)
            output_dir = os.path.join(project_dir, "annotation_project")
            assignments_dir = os.path.join(output_dir, "assignments")
            results_dir = os.path.join(output_dir, "results")
            os.makedirs(assignments_dir)
            os.makedirs(results_dir)

            config = {
                "annotators": [
                    {"id": "alice", "name": "Alice"},
                    {"id": "bob", "name": "Bob"},
                ],
                "output_dir": "annotation_project",
            }
            config_path = os.path.join(project_dir, "annotation_config.yaml")
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(config, f)

            assignments = {
                "alice": ["agree.json", "conflict.json"],
                "bob": ["agree.json", "conflict.json"],
            }
            for annotator_id, docs in assignments.items():
                with open(os.path.join(assignments_dir, f"{annotator_id}.json"), "w", encoding="utf-8") as f:
                    json.dump(docs, f)
                os.makedirs(os.path.join(results_dir, annotator_id))

            agreed_payload = [
                {
                    "quantity": {"text": "10mg", "begin": 0, "end": 4},
                    "eventType": "Medication",
                }
            ]
            conflict_left = [
                {
                    "quantity": {"text": "20mg", "begin": 0, "end": 4},
                    "eventType": "Medication",
                }
            ]
            conflict_right = [
                {
                    "quantity": {"text": "20", "begin": 0, "end": 2},
                    "eventType": "Drug",
                }
            ]

            with open(os.path.join(results_dir, "alice", "agree.json"), "w", encoding="utf-8") as f:
                json.dump(agreed_payload, f)
            with open(os.path.join(results_dir, "bob", "agree.json"), "w", encoding="utf-8") as f:
                json.dump(agreed_payload, f)
            with open(os.path.join(results_dir, "alice", "conflict.json"), "w", encoding="utf-8") as f:
                json.dump(conflict_left, f)
            with open(os.path.join(results_dir, "bob", "conflict.json"), "w", encoding="utf-8") as f:
                json.dump(conflict_right, f)

            report = integrate_project(config_path)

            summary = report["summary"]
            self.assertEqual(summary["total_documents"], 2)
            self.assertEqual(summary["fully_annotated_documents"], 2)
            self.assertEqual(summary["auto_merged_documents"], 1)
            self.assertEqual(summary["documents_with_disagreement"], 1)
            self.assertEqual(summary["documents_pending_annotations"], 0)
            self.assertEqual(summary["overall_pairwise_iaa"], 0.5)
            self.assertNotIn("overall_pairwise_cohens_kappa", summary)

            pair_metrics = report["pairwise_iaa_by_pair"]["alice :: bob"]
            self.assertEqual(pair_metrics["compared_docs"], 2)
            self.assertEqual(pair_metrics["agreed"], 1)
            self.assertEqual(pair_metrics["observed_agreement"], 0.5)
            self.assertNotIn("cohens_kappa", pair_metrics)

            merged_path = os.path.join(output_dir, "integration", "merged", "agree.json")
            self.assertTrue(os.path.exists(merged_path))

            with open(merged_path, "r", encoding="utf-8") as f:
                merged_payload = json.load(f)

            self.assertEqual(merged_payload, agreed_payload)
            self.assertFalse(
                os.path.exists(os.path.join(output_dir, "integration", "merged", "conflict.json"))
            )

    def test_integrate_project_keeps_unresolved_flagged_docs_in_conflict_bundles(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_dir = os.path.join(tmp_dir, "project")
            os.makedirs(project_dir)
            output_dir = os.path.join(project_dir, "annotation_project")
            assignments_dir = os.path.join(output_dir, "assignments")
            results_dir = os.path.join(output_dir, "results")
            os.makedirs(assignments_dir)
            os.makedirs(results_dir)

            config = {
                "annotators": [
                    {"id": "alice", "name": "Alice"},
                    {"id": "bob", "name": "Bob"},
                ],
                "output_dir": "annotation_project",
            }
            config_path = os.path.join(project_dir, "annotation_config.yaml")
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(config, f)

            assignments = {
                "alice": ["needs_resolution.json"],
                "bob": ["needs_resolution.json"],
            }
            payload = [
                {
                    "quantity": {"text": "10mg", "begin": 0, "end": 4},
                    "eventType": "Medication",
                }
            ]

            for annotator_id, docs in assignments.items():
                with open(os.path.join(assignments_dir, f"{annotator_id}.json"), "w", encoding="utf-8") as f:
                    json.dump(docs, f)

                annotator_dir = os.path.join(results_dir, annotator_id)
                os.makedirs(annotator_dir)
                os.makedirs(os.path.join(annotator_dir, "review_state"))

                with open(os.path.join(annotator_dir, "needs_resolution.json"), "w", encoding="utf-8") as f:
                    json.dump(payload, f)
                with open(
                    os.path.join(annotator_dir, "review_state", "needs_resolution.json"),
                    "w",
                    encoding="utf-8",
                ) as f:
                    json.dump(
                        {
                            "doc_id": "needs_resolution.json",
                            "reviewed_pair_count": 1,
                            "unresolved_pair_count": 1,
                            "reviewed_pairs": [
                                {
                                    "pair_index": 0,
                                    "status": "flagged",
                                    "flagged_fields": ["quantity"],
                                }
                            ],
                        },
                        f,
                    )

            report = integrate_project(config_path)

            document = report["documents"][0]
            self.assertEqual(document["status"], "conflict")
            self.assertEqual(document["conflict_reasons"], ["unresolved_flagged_pairs"])
            self.assertEqual(sorted(document["unresolved_annotators"]), ["alice", "bob"])
            self.assertTrue(os.path.exists(document["conflict_bundle"]))

    def test_resolve_conflict_writes_resolved_output_and_follow_up_report(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_dir = os.path.join(tmp_dir, "project")
            os.makedirs(project_dir)
            output_dir = os.path.join(project_dir, "annotation_project")
            assignments_dir = os.path.join(output_dir, "assignments")
            results_dir = os.path.join(output_dir, "results")
            os.makedirs(assignments_dir)
            os.makedirs(results_dir)

            config = {
                "annotators": [
                    {"id": "alice", "name": "Alice"},
                    {"id": "bob", "name": "Bob"},
                ],
                "output_dir": "annotation_project",
            }
            config_path = os.path.join(project_dir, "annotation_config.yaml")
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(config, f)

            assignments = {
                "alice": ["conflict.json"],
                "bob": ["conflict.json"],
            }
            for annotator_id, docs in assignments.items():
                with open(os.path.join(assignments_dir, f"{annotator_id}.json"), "w", encoding="utf-8") as f:
                    json.dump(docs, f)
                os.makedirs(os.path.join(results_dir, annotator_id))

            left_payload = [
                {
                    "quantity": {"text": "20mg", "begin": 0, "end": 4},
                    "eventType": "Medication",
                }
            ]
            right_payload = [
                {
                    "quantity": {"text": "20", "begin": 0, "end": 2},
                    "eventType": "Drug",
                }
            ]

            with open(os.path.join(results_dir, "alice", "conflict.json"), "w", encoding="utf-8") as f:
                json.dump(left_payload, f)
            with open(os.path.join(results_dir, "bob", "conflict.json"), "w", encoding="utf-8") as f:
                json.dump(right_payload, f)

            integrate_project(config_path)
            result = resolve_conflict(
                config_path,
                "conflict.json",
                source_annotator="alice",
                reviewer="qa-reviewer",
                note="Keep Alice output as final resolution",
            )

            self.assertTrue(os.path.exists(result["resolved_output"]))
            self.assertTrue(os.path.exists(result["resolution_report"]))

            with open(result["resolved_output"], "r", encoding="utf-8") as f:
                resolved_payload = json.load(f)
            self.assertEqual(resolved_payload, left_payload)

            with open(result["resolution_report"], "r", encoding="utf-8") as f:
                resolution_report = json.load(f)

            self.assertEqual(len(resolution_report["resolved_documents"]), 1)
            self.assertEqual(resolution_report["resolved_documents"][0]["doc_id"], "conflict.json")
            self.assertEqual(
                resolution_report["resolved_documents"][0]["source_annotator"],
                "alice",
            )


if __name__ == "__main__":
    unittest.main()
