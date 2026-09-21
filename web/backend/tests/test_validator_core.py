import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import unittest
from ner_validator_core import NERValidatorCore

class TestNERValidatorCore(unittest.TestCase):
    def setUp(self):
        # Sample data to test event-based NERValidatorCore
        self.doc_id = "doc1"
        self.data = {
            self.doc_id: {
                "text": "Microsoft CEO Satya Nadella and Google CEO Sundar Pichai met in Seattle.",
                "real_events": [
                    {
                        "event_id": "r1",
                        "company": {"text": "Microsoft", "begin": 0, "end": 9},
                        "person": {"text": "Satya Nadella", "begin": 14, "end": 26},
                        "type": "Meeting"
                    },
                    {
                        "event_id": "r2",
                        "company": {"text": "Google", "begin": 31, "end": 37},
                        "person": {"text": "Sundar Pichai", "begin": 42, "end": 54},
                        "type": "Meeting"
                    }
                ],
                "model_events": [
                    {
                        "event_id": "m1",
                        "company": {"text": "Microsoft", "begin": 0, "end": 9},
                        "person": {"text": "Satya Nadella", "begin": 14, "end": 26},
                        "type": "Meeting"
                    }, # This is a perfect match with r1
                    {
                        "event_id": "m2",
                        "company": {"text": "Google", "begin": 31, "end": 37},
                        "person": {"text": "Sundar", "begin": 42, "end": 48}, # Discrepancy: partial match
                        "type": "Meeting"
                    }
                ]
            }
        }
        
        # We specify a simple schema matching our events
        self.schema = {
            "fields": [
                {"path": "company"},
                {"path": "person"},
                {"path": "type"} # Will be detected as value, others as spans
            ]
        }
        
    def test_find_event_differences(self):
        """Test that identical events are filtered and discrepant events are paired."""
        validator = NERValidatorCore(self.data, schema=self.schema)
        
        # r1 and m1 match perfectly, so they shouldn't be in current_differences
        self.assertIn(self.doc_id, validator.current_differences)
        diffs = validator.current_differences[self.doc_id]['pairs']
        
        # There should be exactly 1 pair with discrepancies (r2, m2)
        self.assertEqual(len(diffs), 1)
        r2, m2 = diffs[0]
        
        # r2 and m2 overlap, they should be paired
        self.assertEqual(r2['person']['text'], "Sundar Pichai")
        self.assertEqual(m2['person']['text'], "Sundar")

    def test_get_next_example(self):
        """Test getting next pair to validate."""
        validator = NERValidatorCore(self.data, schema=self.schema)
        
        example = validator.get_next_example()
        self.assertFalse(example.get('completed', False))
        
        self.assertEqual(example['doc_id'], self.doc_id)
        self.assertEqual(example['real_event']['person']['text'], "Sundar Pichai")
        self.assertEqual(example['model_event']['person']['text'], "Sundar")
        
        self.assertEqual(example['total_remaining'], 1)

    def test_submit_validation(self):
        """Test submitting validation updates the counts and decisions."""
        validator = NERValidatorCore(self.data, schema=self.schema)
        
        example = validator.get_next_example()
        
        # Accept the model event as correct
        validator.submit_validation(
            example['doc_id'],
            example['pair_index'],
            example['model_event']
        )
        
        # Everything should be complete now
        next_example = validator.get_next_example()
        self.assertTrue(next_example.get('completed', False))
        
        doc_info = validator.current_differences[self.doc_id]
        self.assertEqual(doc_info['validated_count'], 1)

    def test_flagged_decisions_are_persisted_as_unresolved_and_can_be_undone(self):
        validator = NERValidatorCore(self.data, schema=self.schema)

        example = validator.get_next_example()
        validator.submit_validation(
            example['doc_id'],
            example['pair_index'],
            None,
            flagged_fields=['person'],
        )

        review_state = validator.get_doc_review_state(self.doc_id)
        self.assertEqual(review_state['unresolved_pair_count'], 1)
        self.assertEqual(review_state['reviewed_pairs'][0]['status'], 'flagged')
        self.assertEqual(review_state['reviewed_pairs'][0]['flagged_fields'], ['person'])
        self.assertEqual(len(validator.get_doc_results(self.doc_id)), 1)

        undone = validator.undo_last_validation()
        self.assertEqual(undone['doc_id'], self.doc_id)
        self.assertEqual(undone['pair_index'], example['pair_index'])

        review_state_after_undo = validator.get_doc_review_state(self.doc_id)
        self.assertEqual(review_state_after_undo['unresolved_pair_count'], 0)
        self.assertEqual(review_state_after_undo['reviewed_pairs'], [])
        
    def test_get_final_tags(self):
        """Test generating final curated tags containing perfect matches + validations."""
        validator = NERValidatorCore(self.data, schema=self.schema)
        
        example = validator.get_next_example()
        
        # We will create a hybrid event to simulate the user combining data
        hybrid_event = {
            "company": {"text": "Google", "begin": 31, "end": 37},
            "person": {"text": "Sundar Pichai", "begin": 42, "end": 54}, # Chose real
            "type": "Virtual Meeting" # Re-typed it
        }
        
        validator.submit_validation(
            example['doc_id'],
            example['pair_index'],
            hybrid_event
        )
        
        results = validator.get_final_tags()
        final_events = results[self.doc_id]
        
        self.assertEqual(len(final_events), 2)
        
        # One is the perfect match
        self.assertEqual(final_events[0]['person']['text'], "Satya Nadella")
        # Second is our hybrid decision
        self.assertEqual(final_events[1]['person']['text'], "Sundar Pichai")
        self.assertEqual(final_events[1]['type'], "Virtual Meeting")

    def test_unmatched_real_events_are_auto_kept_and_model_only_events_are_dropped(self):
        """Non-overlapping one-sided events should not appear as UI review pairs."""
        doc_id = "doc_unmatched"
        data = {
            doc_id: {
                "text": "Alpha met Beta yesterday. Gamma met Delta today.",
                "real_events": [
                    {
                        "event_id": "r1",
                        "company": {"text": "Alpha", "begin": 0, "end": 5},
                        "person": {"text": "Beta", "begin": 10, "end": 14},
                        "type": "Meeting"
                    }
                ],
                "model_events": [
                    {
                        "event_id": "m1",
                        "company": {"text": "Gamma", "begin": 26, "end": 31},
                        "person": {"text": "Delta", "begin": 36, "end": 41},
                        "type": "Meeting"
                    }
                ]
            }
        }

        validator = NERValidatorCore(data, schema=self.schema)

        # No overlapping pair should be shown to the annotator.
        self.assertNotIn(doc_id, validator.current_differences)

        example = validator.get_next_example()
        self.assertTrue(example.get('completed', False))

        final_events = validator.get_doc_results(doc_id)
        self.assertEqual(len(final_events), 1)
        self.assertEqual(final_events[0]['company']['text'], "Alpha")
        self.assertEqual(final_events[0]['person']['text'], "Beta")

if __name__ == '__main__':
    unittest.main()
