import importlib
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BACKEND_DIR = os.path.join(ROOT_DIR, "web", "backend")

sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, BACKEND_DIR)


from scripts.orchestrate import assign_documents_sequential, find_documents_with_discrepancies
from scripts.orchestrate import (
    assign_documents_by_plan,
    resolve_validation_document_count,
    select_documents_for_assignment,
    split_documents_for_validation,
)
from prepare_comparison_data import get_text_content


def import_web_app_module():
    """Import web_app with an eel stub so tests stay headless."""
    sys.modules["eel"] = types.SimpleNamespace(
        init=lambda *args, **kwargs: None,
        expose=lambda fn: fn,
        start=lambda *args, **kwargs: None,
    )
    if "web_app" in sys.modules:
        return importlib.reload(sys.modules["web_app"])
    return importlib.import_module("web_app")


class TestMultiAnnotatorWorkflow(unittest.TestCase):
    def test_web_asset_directory_is_resolved_from_module_location(self):
        web_app = import_web_app_module()

        self.assertTrue(os.path.exists(os.path.join(web_app.WEB_DIR, "index.html")))

    def test_source_text_lookup_is_independent_of_current_working_directory(self):
        # Reads from the bundled synthetic corpus rather than a document of the
        # experimental one, which is third-party data and is not distributed
        # with the public release. A test that depends on undistributable data
        # can only fail for anyone outside the project.
        import prepare_comparison_data

        sample_source = os.path.join(ROOT_DIR, "data", "sample", "source")
        original_dir = prepare_comparison_data.SOURCE_TEXT_DIR
        original_cwd = os.getcwd()
        try:
            prepare_comparison_data.SOURCE_TEXT_DIR = pathlib.Path(sample_source)

            os.chdir(ROOT_DIR)
            from_root = get_text_content("sample_001.json")

            os.chdir(BACKEND_DIR)
            from_backend = get_text_content("sample_001.json")
        finally:
            os.chdir(original_cwd)
            prepare_comparison_data.SOURCE_TEXT_DIR = original_dir

        self.assertIsNotNone(from_root)
        self.assertEqual(from_root, from_backend)

    def test_only_discrepant_docs_are_assignable(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            ground_truth_dir = os.path.join(tmp_dir, "ground_truth")
            model_tags_dir = os.path.join(tmp_dir, "model_tags")
            os.makedirs(ground_truth_dir)
            os.makedirs(model_tags_dir)

            schema = {"fields": [{"path": "quantity"}, {"path": "eventType"}]}

            same_payload = [
                {
                    "quantity": {"text": "10mg", "begin": 0, "end": 4},
                    "eventType": "Medication",
                }
            ]
            different_gt_payload = [
                {
                    "quantity": {"text": "25mg", "begin": 0, "end": 4},
                    "eventType": "Medication",
                }
            ]
            different_model_payload = [
                {
                    "quantity": {"text": "25", "begin": 0, "end": 2},
                    "eventType": "Drug",
                }
            ]

            with open(os.path.join(ground_truth_dir, "same.json"), "w", encoding="utf-8") as f:
                json.dump(same_payload, f)
            with open(os.path.join(model_tags_dir, "same.json"), "w", encoding="utf-8") as f:
                json.dump(same_payload, f)
            with open(os.path.join(ground_truth_dir, "different.json"), "w", encoding="utf-8") as f:
                json.dump(different_gt_payload, f)
            with open(os.path.join(model_tags_dir, "different.json"), "w", encoding="utf-8") as f:
                json.dump(different_model_payload, f)

            discrepant_docs = find_documents_with_discrepancies(
                ground_truth_dir,
                model_tags_dir,
                schema=schema,
            )

            self.assertEqual(discrepant_docs, ["different.json"])

    def test_assignments_cover_every_doc_with_balanced_load(self):
        documents = [f"doc_{idx}.json" for idx in range(9)]
        annotators = [{"id": "a"}, {"id": "b"}, {"id": "c"}]

        assignments = assign_documents_sequential(documents, annotators, redundancy=2)

        counts = {annotator_id: len(docs) for annotator_id, docs in assignments.items()}
        self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)

        per_doc_counts = {doc_id: 0 for doc_id in documents}
        for docs in assignments.values():
            for doc_id in docs:
                per_doc_counts[doc_id] += 1

        self.assertTrue(all(count == 2 for count in per_doc_counts.values()))

    def test_document_limit_and_validation_percentage_support_mixed_redundancy(self):
        documents = [f"doc_{idx}.json" for idx in range(20)]
        annotators = [{"id": "a"}, {"id": "b"}, {"id": "c"}]

        selected_documents = select_documents_for_assignment(
            documents,
            document_limit=10,
            strategy="sequential",
        )
        validation_document_count = resolve_validation_document_count(
            len(selected_documents),
            validation_document_percentage=10,
        )
        validation_documents, standard_documents = split_documents_for_validation(
            selected_documents,
            validation_document_count,
        )

        document_plan = [(doc_id, 3) for doc_id in validation_documents]
        document_plan.extend((doc_id, 1) for doc_id in standard_documents)
        assignments = assign_documents_by_plan(document_plan, annotators)

        self.assertEqual(len(selected_documents), 10)
        self.assertEqual(validation_documents, ["doc_0.json"])
        self.assertEqual(len(standard_documents), 9)

        per_doc_counts = {doc_id: 0 for doc_id in selected_documents}
        for docs in assignments.values():
            for doc_id in docs:
                per_doc_counts[doc_id] += 1

        self.assertEqual(per_doc_counts["doc_0.json"], 3)
        for doc_id in standard_documents:
            self.assertEqual(per_doc_counts[doc_id], 1)

    def test_flag_only_submissions_advance_and_persist_review_state(self):
        web_app = import_web_app_module()

        with tempfile.TemporaryDirectory() as tmp_dir:
            ground_truth_dir = os.path.join(tmp_dir, "ground_truth")
            model_tags_dir = os.path.join(tmp_dir, "model_tags")
            corrected_dir = os.path.join(tmp_dir, "results")
            os.makedirs(ground_truth_dir)
            os.makedirs(model_tags_dir)
            os.makedirs(corrected_dir)

            schema = {"fields": [{"path": "quantity"}, {"path": "eventType"}]}

            ground_truth_docs = {
                "assigned.json": [
                    {
                        "quantity": {"text": "10mg", "begin": 0, "end": 4},
                        "eventType": "Medication",
                    },
                    {
                        "quantity": {"text": "20mg", "begin": 10, "end": 14},
                        "eventType": "Medication",
                    }
                ],
                "unassigned.json": [
                    {
                        "quantity": {"text": "25mg", "begin": 0, "end": 4},
                        "eventType": "Medication",
                    }
                ],
            }
            model_docs = {
                "assigned.json": [
                    {
                        "quantity": {"text": "10", "begin": 0, "end": 2},
                        "eventType": "Medication",
                    },
                    {
                        "quantity": {"text": "20", "begin": 10, "end": 12},
                        "eventType": "Drug",
                    }
                ],
                "unassigned.json": [
                    {
                        "quantity": {"text": "25", "begin": 0, "end": 2},
                        "eventType": "Drug",
                    }
                ],
            }

            for filename, payload in ground_truth_docs.items():
                with open(os.path.join(ground_truth_dir, filename), "w", encoding="utf-8") as f:
                    json.dump(payload, f)
            for filename, payload in model_docs.items():
                with open(os.path.join(model_tags_dir, filename), "w", encoding="utf-8") as f:
                    json.dump(payload, f)

            validator_ui = web_app.NERValidatorUI(
                ground_truth_dir,
                model_tags_dir,
                os.path.join(tmp_dir, "output.json"),
                schema=schema,
                annotator_name="Annotator A",
            )
            validator_ui.set_corrected_dir(corrected_dir)
            validator_ui.set_doc_filter({"assigned.json"})

            initial = validator_ui.initialize()

            self.assertEqual(initial["doc_id"], "assigned.json")
            self.assertEqual(initial["annotator_name"], "Annotator A")
            self.assertNotEqual(initial["real_event"]["quantity"]["text"], initial["model_event"]["quantity"]["text"])

            flagged_result = validator_ui.submit_validation(
                "assigned.json",
                initial["pair_index"],
                None,
                ["quantity"],
                "unclear quantity in source text",
            )

            self.assertFalse(flagged_result.get("completed", False))
            self.assertEqual(flagged_result["doc_id"], "assigned.json")
            self.assertNotEqual(flagged_result["pair_index"], initial["pair_index"])

            result = validator_ui.submit_validation(
                "assigned.json",
                flagged_result["pair_index"],
                flagged_result["real_event"],
                [],
                "accepted after manual check",
            )

            self.assertTrue(result.get("completed", False))
            flagged_log_path = os.path.join(corrected_dir, "flagged.log")
            self.assertTrue(os.path.exists(flagged_log_path))

            with open(flagged_log_path, "r", encoding="utf-8") as f:
                log_contents = f.read()

            self.assertIn("assigned.json", log_contents)
            self.assertIn("quantity", log_contents)
            self.assertIn("unclear quantity in source text", log_contents)

            review_comments_log_path = os.path.join(corrected_dir, "review_comments.log")
            self.assertTrue(os.path.exists(review_comments_log_path))

            with open(review_comments_log_path, "r", encoding="utf-8") as f:
                review_comment_contents = f.read()

            self.assertIn("assigned.json", review_comment_contents)
            self.assertIn("accepted after manual check", review_comment_contents)

            review_state_path = os.path.join(corrected_dir, "review_state", "assigned.json")
            self.assertTrue(os.path.exists(review_state_path))

            with open(review_state_path, "r", encoding="utf-8") as f:
                review_state = json.load(f)

            self.assertEqual(review_state["unresolved_pair_count"], 1)
            self.assertEqual(
                [pair["status"] for pair in review_state["reviewed_pairs"]],
                ["flagged", "accepted"],
            )


if __name__ == "__main__":
    unittest.main()
