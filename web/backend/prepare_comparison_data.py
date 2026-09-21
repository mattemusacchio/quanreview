import json
import os
import glob
import argparse
import yaml
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_TEXT_DIR = PROJECT_ROOT / "data" / "sample" / "source"

# Where the original .txt documents live. Overridable so a corpus can ship its
# source texts next to its annotations instead of in the project-wide location
# (the sample corpus in data/sample/ does exactly that).
SOURCE_TEXT_DIR = Path(
    os.environ.get("NER_SOURCE_TEXT_DIR", DEFAULT_SOURCE_TEXT_DIR)
)


def _get_nested(obj, path):
    """Traverse a nested dict using a '->'-separated path string."""
    if not path or obj is None:
        return None
    cur = obj
    for part in path.split('->'):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def parse_events_json(file_path):
    """Parse a JSON file into a list of event objects."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return []

    if not isinstance(data, list):
        return []

    events = []
    for i, evt in enumerate(data):
        event_copy = dict(evt)
        event_copy['event_id'] = f"event_{i}"
        events.append(event_copy)
        
    return events


def _is_span_value(value):
    return (
        isinstance(value, dict)
        and 'text' in value
        and (value.get('end') is not None or value.get('begin') is not None or value.get('start') is not None)
    )


def serialize_event_for_comparison(event, schema):
    """Creates a sortable representation of the event to detect differences
    based on the provided schema.
    """
    if not schema or 'fields' not in schema or not schema['fields']:
        return ""
        
    filtered = {k: v for k, v in event.items() if k != 'event_id'}
    parts = []

    for fd in schema['fields']:
        path = fd['path']
        val = _get_nested(filtered, path)
        if val is None:
            continue
        if _is_span_value(val):
            begin = val.get('begin') if val.get('begin') is not None else val.get('start')
            parts.append(f"{path}:{begin}-{val['end']}:{val['text']}")
        else:
            parts.append(f"{path}:{val}")

    return "|".join(parts)


def get_text_content(json_filename):
    """Get the original text content from annotations_and_sources/source."""
    source_filename = json_filename.replace(".json", ".txt")
    source_path = SOURCE_TEXT_DIR / source_filename

    if source_path.exists():
        with open(source_path, 'r', encoding='utf-8') as f:
            return f.read()

    return None


def prepare_comparison_data(ground_truth_dir, model_tags_dir, schema=None, doc_filter=None):
    """Build and return the comparison data dict (no file I/O).

    Args:
        ground_truth_dir: Path to directory containing ground truth JSON files.
        model_tags_dir:   Path to directory containing model tag JSON files.
        schema:           Optional parsed schema dict (already loaded).
        doc_filter:       Optional set of filenames to restrict processing to.
                          When provided, only documents in this set are loaded.

    Returns:
        dict mapping doc_id -> {text, real_events, model_events}
    """
    # Handle nested dir convention
    nested = os.path.join(ground_truth_dir, "ground_truth_isi")
    if os.path.exists(nested):
        ground_truth_dir = nested

    gt_files = {os.path.basename(f) for f in glob.glob(os.path.join(ground_truth_dir, "*.json"))}
    model_files = {os.path.basename(f) for f in glob.glob(os.path.join(model_tags_dir, "*.json"))}

    common_files = gt_files.intersection(model_files)

    # Apply document filter if provided (for per-annotator sessions)
    if doc_filter is not None:
        common_files = common_files.intersection(doc_filter)
        print(f"Filtered to {len(common_files)} documents (from annotator assignment).")
    else:
        print(f"Found {len(common_files)} common files between annotated tags and model predicted tags.")

    input_data = {}
    count = 0
    discrepancy_count = 0

    for filename in sorted(common_files):
        count += 1
        doc_id = filename

        real_events = parse_events_json(os.path.join(ground_truth_dir, filename))
        model_events = parse_events_json(os.path.join(model_tags_dir, filename))

        real_sigs = {serialize_event_for_comparison(e, schema) for e in real_events}
        model_sigs = {serialize_event_for_comparison(e, schema) for e in model_events}

        if real_sigs != model_sigs:
            text = get_text_content(filename)
            if not text:
                print(f"WARNING: Text file not found for {filename}. Using placeholder.")
                text = "[Source text missing]"
            input_data[doc_id] = {
                "text": text,
                "real_events": real_events,
                "model_events": model_events,
            }
            discrepancy_count += 1

    print(f"Processed {count} common files.")
    print(f"Found {discrepancy_count} documents with discrepancies.")
    return input_data


def main():
    parser = argparse.ArgumentParser(description="Prepare comparison data for NER validator.")
    parser.add_argument('--ground-truth-dir', default="../../data/sample/ground_truth",
                        help="Directory containing ground truth JSON files")
    parser.add_argument('--model-tags-dir', default="../../data/sample/model_tags",
                        help="Directory containing model tag JSON files")
    parser.add_argument('--output-file', default="../../data/comparison_input.json",
                        help="Path for the generated comparison input JSON")
    parser.add_argument('--schema', default=None,
                        help="Path to a schema JSON file specifying which fields to compare")
    args = parser.parse_args()

    schema = None
    if args.schema:
        try:
            with open(args.schema, 'r', encoding='utf-8') as f:
                schema = yaml.safe_load(f)
            print(f"Loaded schema from {args.schema} with {len(schema.get('fields', []))} field(s).")
        except Exception as e:
            print(f"Warning: could not load schema from {args.schema}: {e}")

    data = prepare_comparison_data(args.ground_truth_dir, args.model_tags_dir, schema=schema)

    with open(args.output_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)
    print(f"Generated {args.output_file} successfully.")


if __name__ == "__main__":
    main()
