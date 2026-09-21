import eel
import json
import sys
import os
import argparse
import yaml
from pathlib import Path
from ner_validator_core import NERValidatorCore
from prepare_comparison_data import prepare_comparison_data


WEB_DIR = Path(__file__).resolve().parent.parent


class NERValidatorUI:
    def __init__(self, ground_truth_dir, model_tags_dir, output_file, schema=None, annotator_name=None):
        self.ground_truth_dir = ground_truth_dir
        self.model_tags_dir = model_tags_dir
        self.output_file = output_file
        self.schema = schema
        self.corrected_dir = None
        self.validator = None
        self.annotator_name = annotator_name
        self.doc_filter = None

    def set_corrected_dir(self, corrected_dir):
        self.corrected_dir = corrected_dir
        if corrected_dir and not os.path.exists(corrected_dir):
            os.makedirs(corrected_dir)

    def set_doc_filter(self, doc_filter):
        """Set a document filter for per-annotator sessions.

        Args:
            doc_filter: A set of filenames to restrict which documents are loaded.
        """
        self.doc_filter = doc_filter

    def _get_log_dir(self):
        """Return the directory where runtime logs should be stored."""
        if self.corrected_dir:
            return self.corrected_dir
        return os.path.dirname(os.path.abspath(self.output_file))

    def _get_review_state_dir(self):
        """Return the directory where per-document review metadata is stored."""
        base_dir = self.corrected_dir or os.path.dirname(os.path.abspath(self.output_file))
        review_state_dir = os.path.join(base_dir, "review_state")
        os.makedirs(review_state_dir, exist_ok=True)
        return review_state_dir

    def _get_processed_doc_ids(self):
        """Get list of already processed document IDs."""
        if not self.corrected_dir:
            return set()

        processed = set()
        for filename in os.listdir(self.corrected_dir):
            if filename.endswith('.json'):
                doc_id = filename.replace('.json', '')
                if doc_id in self.validator.data:
                    processed.add(doc_id)
                elif filename in self.validator.data:
                    processed.add(filename)
        return processed

    def initialize(self):
        """Load data directly from source directories and initialize the validator."""
        try:
            data = prepare_comparison_data(
                self.ground_truth_dir, self.model_tags_dir,
                schema=self.schema, doc_filter=self.doc_filter
            )

            self.validator = NERValidatorCore(data, schema=self.schema)

            if self.corrected_dir:
                processed_ids = self._get_processed_doc_ids()
                print(f"Found {len(processed_ids)} already corrected documents. Skipping them.")
                self.validator.mark_docs_as_processed(processed_ids)

            initial = self.validator.get_next_example()
            initial['schema'] = self.schema
            if self.annotator_name:
                initial['annotator_name'] = self.annotator_name
            return initial
        except ValueError as e:
            return {'error': f'Invalid data format: {str(e)}'}
        except Exception as e:
            return {'error': f'Error initializing validator: {str(e)}'}
    
    def submit_validation(self, doc_id, pair_index, accepted_event, flagged_fields=None, comment=None):
        """Submit event validation and get next example."""
        if not self.validator:
            return {'error': 'Validator not initialized'}

        try:
            comment = (comment or "").strip()
            skipped = accepted_event is None and not flagged_fields
            if flagged_fields or comment or skipped:
                log_dir = self._get_log_dir()
                if flagged_fields:
                    log_name = 'flagged.log'
                elif skipped:
                    log_name = 'skipped.log'
                else:
                    log_name = 'review_comments.log'
                log_file = os.path.join(log_dir, log_name)
                with open(log_file, 'a', encoding='utf-8') as f:
                    if flagged_fields:
                        fields_str = ', '.join(flagged_fields)
                        reason = f"Flagged fields [{fields_str}]"
                    elif skipped:
                        reason = "Skipped (no side selected, no flag)"
                    else:
                        reason = "Reviewer comment"
                    f.write(f"File: {doc_id} | Pair: {pair_index} | Reason: {reason}\n")
                    if comment:
                        for line in comment.splitlines():
                            f.write(f"# {line}\n")

            result = self.validator.submit_validation(
                doc_id,
                pair_index,
                accepted_event,
                flagged_fields=flagged_fields,
            )
            
            if result.get('completed', False):
                self.save_results()
                result['output_file'] = self.output_file

            if self.corrected_dir and self.validator.is_doc_complete(doc_id):
                self.save_doc_result(doc_id)
                self.save_doc_review_state(doc_id)
            
            return result
        except Exception as e:
            return {'error': str(e)}

    def undo_last_validation(self):
        """Undo the last validation and return the previous example."""
        if not self.validator:
            return {'error': 'Validator not initialized'}
        
        try:
            return self.validator.undo_last_validation()
        except Exception as e:
            return {'error': str(e)}
    
    def save_results(self):
        """Save final results to output file."""
        if not self.validator:
            return {'error': 'Validator not initialized'}
        
        try:
            results = self.validator.get_final_tags()
            with open(self.output_file, 'w') as f:
                json.dump(results, f, indent=4)

            if not self.corrected_dir:
                for doc_id in results:
                    self.save_doc_review_state(doc_id)
        except Exception as e:
            return {'error': str(e)}

    def save_doc_result(self, doc_id):
        """Save result for a single document."""
        try:
            final_events = self.validator.get_doc_results(doc_id)
            
            clean_events = []
            for ev in final_events:
                clean_ev = {k: v for k, v in ev.items() if k != 'event_id'}
                clean_events.append(clean_ev)
            
            output_filename = f"{doc_id}" if doc_id.endswith('.json') else f"{doc_id}.json"
            output_path = os.path.join(self.corrected_dir, output_filename)
            
            with open(output_path, 'w') as f:
                json.dump(clean_events, f, indent=4)
            print(f"Saved corrected document to: {output_path}")
        except Exception as e:
            print(f"Error saving document {doc_id}: {e}")

    def save_doc_review_state(self, doc_id):
        """Persist unresolved/flagged review metadata for a single document."""
        try:
            review_state = self.validator.get_doc_review_state(doc_id)
            review_state_path = os.path.join(
                self._get_review_state_dir(),
                f"{doc_id}" if doc_id.endswith('.json') else f"{doc_id}.json",
            )
            with open(review_state_path, 'w', encoding='utf-8') as f:
                json.dump(review_state, f, indent=4)
            print(f"Saved review state to: {review_state_path}")
        except Exception as e:
            print(f"Error saving review state for {doc_id}: {e}")


# Maintain the global validator instance
validator = None

def parse_args():
    """Parse command line arguments.
    
    Supports both the new argparse-based interface and the legacy positional
    argument interface for backward compatibility.
    """
    # Check if the user is using the legacy positional interface
    # Legacy: python web_app.py <gt_dir> <model_dir> <output_file> [corrected_dir] [schema_file]
    # New args start with '--', legacy args don't
    has_flags = any(arg.startswith('--') for arg in sys.argv[1:])
    
    if not has_flags and len(sys.argv) >= 4:
        # Legacy positional interface
        class LegacyArgs:
            pass
        args = LegacyArgs()
        args.ground_truth_dir = sys.argv[1]
        args.model_tags_dir = sys.argv[2]
        args.output_file = sys.argv[3]
        args.corrected_dir = sys.argv[4] if len(sys.argv) > 4 else None
        args.schema = sys.argv[5] if len(sys.argv) > 5 else None
        args.annotator = None
        args.project_dir = None
        return args

    parser = argparse.ArgumentParser(
        description="NER Tag Validator Web Application",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Standard mode (all documents):
  python web_app.py --ground-truth-dir ../../data/sample/ground_truth --model-tags-dir ../../data/sample/model_tags --output-file ../../data/output.json --corrected-dir ../../data/corrected_ground_truth --schema ../../comparison_schema.yaml

  # Per-annotator mode (filtered to assigned docs):
  python web_app.py --ground-truth-dir ../../data/sample/ground_truth --model-tags-dir ../../data/sample/model_tags --output-file ../../data/annotation_project/results/annotator_a/output.json --corrected-dir ../../data/annotation_project/results/annotator_a --schema ../../comparison_schema.yaml --annotator annotator_a --project-dir ../../data/annotation_project
        """,
    )
    parser.add_argument("--ground-truth-dir", required=True,
                        help="Directory containing ground truth JSON files")
    parser.add_argument("--model-tags-dir", required=True,
                        help="Directory containing model tag JSON files")
    parser.add_argument("--output-file", required=True,
                        help="Path for the generated comparison output JSON")
    parser.add_argument("--corrected-dir", default=None,
                        help="Directory to save per-document corrected outputs")
    parser.add_argument("--schema", default=None,
                        help="Path to a YAML schema file specifying which fields to compare")
    parser.add_argument("--annotator", default=None,
                        help="Annotator ID for per-annotator filtered sessions")
    parser.add_argument("--project-dir", default=None,
                        help="Path to the annotation project directory (contains assignments/)")
    return parser.parse_args()


def load_annotator_assignment(project_dir: str, annotator_id: str) -> set[str]:
    """Load the document assignment manifest for an annotator.

    Args:
        project_dir: Path to the annotation project directory.
        annotator_id: The annotator's ID.

    Returns:
        Set of document filenames assigned to this annotator.
    """
    manifest_path = os.path.join(project_dir, "assignments", f"{annotator_id}.json")
    if not os.path.exists(manifest_path):
        print(f"ERROR: Assignment manifest not found: {manifest_path}")
        print("  Run 'python scripts/orchestrate.py' first to generate assignments.")
        sys.exit(1)

    with open(manifest_path, "r", encoding="utf-8") as f:
        docs = json.load(f)

    print(f"Loaded {len(docs)} document assignments for annotator '{annotator_id}'.")
    return set(docs)


def get_annotator_display_name(project_dir: str, annotator_id: str) -> str:
    """Get the display name for an annotator from the project config."""
    config_path = os.path.join(project_dir, "config_snapshot.yaml")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            for a in config.get("annotators", []):
                if a["id"] == annotator_id:
                    return a.get("name", annotator_id)
        except Exception:
            pass
    return annotator_id


def main():
    """Main entry point for the web application.

    Usage (legacy):
        python web_app.py <ground_truth_dir> <model_tags_dir> <output_file> [corrected_dir] [schema_file]

    Usage (new):
        python web_app.py --ground-truth-dir <dir> --model-tags-dir <dir> --output-file <file> [options]
    """
    if len(sys.argv) < 2:
        print("Usage: python web_app.py --ground-truth-dir <dir> --model-tags-dir <dir> --output-file <file> [options]")
        print("       python web_app.py <ground_truth_dir> <model_tags_dir> <output_file> [corrected_dir] [schema_file]")
        sys.exit(1)

    args = parse_args()

    schema = None
    if args.schema:
        try:
            with open(args.schema, 'r', encoding='utf-8') as f:
                schema = yaml.safe_load(f)
            print(f"Loaded schema from {args.schema} with {len(schema.get('fields', []))} field(s).")
        except Exception as e:
            print(f"Warning: could not load schema from {args.schema}: {e}")

    # Determine annotator info
    annotator_name = None
    doc_filter = None

    if args.annotator:
        if not args.project_dir:
            print("ERROR: --project-dir is required when using --annotator.")
            sys.exit(1)

        doc_filter = load_annotator_assignment(args.project_dir, args.annotator)
        annotator_name = get_annotator_display_name(args.project_dir, args.annotator)
        print(f"Running in annotator mode: {annotator_name} ({args.annotator})")

    eel.init(str(WEB_DIR))

    global validator
    validator = NERValidatorUI(
        args.ground_truth_dir, args.model_tags_dir, args.output_file,
        schema=schema, annotator_name=annotator_name
    )

    if doc_filter:
        validator.set_doc_filter(doc_filter)

    if args.corrected_dir:
        validator.set_corrected_dir(args.corrected_dir)

    initial_example = validator.initialize()

    @eel.expose
    def get_initial_state():
        return initial_example

    @eel.expose
    def submit_validation(doc_id, pair_index, accepted_event, flagged_fields=None, comment=None):
        global validator
        if not validator:
            return {'error': 'Validator not initialized'}
        return validator.submit_validation(doc_id, pair_index, accepted_event, flagged_fields, comment)

    @eel.expose
    def undo_last_validation():
        global validator
        if not validator:
            return {'error': 'Validator not initialized'}
        return validator.undo_last_validation()

    host = os.environ.get('EEL_HOST', 'localhost')
    port = int(os.environ.get('EEL_PORT', '8765'))
    # When bound to a non-local host, don't try to launch a local browser.
    mode = None if host not in ('localhost', '127.0.0.1') else 'chrome'
    try:
        eel.start('index.html', mode=mode, host=host, port=port)
    except EnvironmentError:
        eel.start('index.html', mode='default', host=host, port=port)

if __name__ == '__main__':
    main()
