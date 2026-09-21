import unittest
import sys
import os

# Add the backend directory to the path so we can import modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ner_validator_core import NERValidatorCore

class TestValidationLogic(unittest.TestCase):
    def setUp(self):
        # Create mock data with one document containing discrepant events
        self.mock_data = {
            "doc1": {
                "text": "The patient took 50mg of Aspirin.",
                "real_events": [
                    {
                        "event_id": "event_0",
                        "quantity": {"text": "50mg", "begin": 17, "end": 21},
                        "eventDescription": {"text": "Aspirin", "begin": 25, "end": 32},
                        "eventType": "Medication"
                    }
                ],
                "model_events": [
                    {
                        "event_id": "event_0",
                        "quantity": {"text": "50", "begin": 17, "end": 19}, # Model missed "mg"
                        "unit": {"text": "mg", "begin": 19, "end": 21},     # Model captured unit separately
                        "eventDescription": {"text": "Aspirin", "begin": 25, "end": 32},
                        "eventType": "Drug" # Model got wrong event type
                    }
                ]
            }
        }
        self.schema = {
            "fields": [
                {"path": "quantity"},
                {"path": "unit"},
                {"path": "eventDescription"},
                {"path": "eventType"}
            ]
        }
        self.validator = NERValidatorCore(self.mock_data, schema=self.schema)
        
        # Verify initialization worked: we should have 1 pair of discrepant events
        self.assertEqual(len(self.validator.docs_with_differences), 1)
        self.assertEqual(len(self.validator.current_differences["doc1"]["pairs"]), 1)

    def test_all_real_acceptance(self):
        """Test accepting the real event exactly as it is."""
        example = self.validator.get_next_example()
        
        # We simulate the UI sending back the exact real event
        accepted_event = dict(example['real_event'])
        
        self.validator.submit_validation("doc1", example['pair_index'], accepted_event)
        
        final_tags = self.validator.get_doc_results("doc1")
        self.assertEqual(len(final_tags), 1)
        
        # Output should exactly match the mocked real event
        self.assertEqual(final_tags[0]["eventType"], "Medication")
        self.assertEqual(final_tags[0]["quantity"]["text"], "50mg")
        self.assertNotIn("unit", final_tags[0])

    def test_all_model_acceptance(self):
        """Test accepting the model event exactly as it is."""
        example = self.validator.get_next_example()
        
        # We simulate the UI sending back the exact model event
        accepted_event = dict(example['model_event'])
        
        self.validator.submit_validation("doc1", example['pair_index'], accepted_event)
        
        final_tags = self.validator.get_doc_results("doc1")
        self.assertEqual(len(final_tags), 1)
        
        # Output should exactly match the mocked model event
        self.assertEqual(final_tags[0]["eventType"], "Drug")
        self.assertEqual(final_tags[0]["quantity"]["text"], "50")
        self.assertEqual(final_tags[0]["unit"]["text"], "mg")

    def test_hybrid_real_base_acceptance(self):
        """Test hybrid acceptance where base is real, but some fields are from model."""
        example = self.validator.get_next_example()
        
        # Simulate hybrid: Real base, but taking 'unit' and 'quantity' from mode
        accepted_event = dict(example['real_event'])
        accepted_event['unit'] = dict(example['model_event']['unit'])
        accepted_event['quantity'] = dict(example['model_event']['quantity'])
        
        self.validator.submit_validation("doc1", example['pair_index'], accepted_event)
        
        final_tags = self.validator.get_doc_results("doc1")
        self.assertEqual(len(final_tags), 1)
        
        # Output should have Real's eventType, but Model's quantity and unit
        self.assertEqual(final_tags[0]["eventType"], "Medication") # From Real
        self.assertEqual(final_tags[0]["quantity"]["text"], "50")  # From Model
        self.assertEqual(final_tags[0]["unit"]["text"], "mg")      # From Model

    def test_hybrid_model_base_acceptance(self):
        """Test hybrid acceptance where base is model, but some fields are from real."""
        example = self.validator.get_next_example()
        
        # Simulate hybrid: Model base, but taking 'eventType' from real
        accepted_event = dict(example['model_event'])
        accepted_event['eventType'] = example['real_event']['eventType']
        
        self.validator.submit_validation("doc1", example['pair_index'], accepted_event)
        
        final_tags = self.validator.get_doc_results("doc1")
        self.assertEqual(len(final_tags), 1)
        
        # Output should have Model's quantity and unit, but Real's event type
        self.assertEqual(final_tags[0]["eventType"], "Medication") # From Real
        self.assertEqual(final_tags[0]["quantity"]["text"], "50")  # From Model
        self.assertEqual(final_tags[0]["unit"]["text"], "mg")      # From Model

if __name__ == '__main__':
    unittest.main()
