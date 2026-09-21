import json
import os
import glob

def load_ground_truth_files(ground_truth_dir):
    """Load all JSON files from Ground Truth directory."""
    files = glob.glob(os.path.join(ground_truth_dir, "*.json"))
    return files

def parse_quantitative_json(file_path):
    """Parse a quantitative JSON file into model tags."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return []

    tags = []
    # Data is a list of events
    if isinstance(data, list):
        for i, event in enumerate(data):
            # Generate a unique event_id for this event object
            event_id = f"event_{i}"
            event_type = event.get("eventType", "Unknown")
            
            # Each event has keys like 'quantity', 'unit', 'eventDescription', etc.
            for label, value in event.items():
                if isinstance(value, dict) and 'text' in value and 'begin' in value and 'end' in value:
                    tags.append({
                        "text": value['text'],
                        "start": value['begin'],
                        "end": value['end'],
                        "label": label,  # e.g., "quantity", "unit"
                        "event_id": event_id,
                        "event_type": event_type
                    })
                elif label == "eventType":
                    # handled above
                    pass
    return tags

def get_text_content(json_filename):
    """Get the original text content from annotations_and_sources/source."""
    # JSON filename is like "a3_63833.txt.json"
    # Source filename should be "a3_63833.txt"
    source_filename = json_filename.replace(".json", "")
    
    # Path relative to script execution
    source_dir = "annotations_and_sources/source"
    source_path = os.path.join(source_dir, source_filename)
    
    if os.path.exists(source_path):
        with open(source_path, 'r', encoding='utf-8') as f:
            return f.read()
            
    return None

def main():
    ground_truth_dir = "GroundTruthISI/GroundTruthISI"
    corrected_dir = "CorrectedGroundTruth"
    output_file = "input.json"
    
    # Create corrected directory if not exists
    if not os.path.exists(corrected_dir):
        os.makedirs(corrected_dir)
        print(f"Created directory: {corrected_dir}")
    
    # Get list of already corrected files (just the basenames without processed extension if any, or matching IDs)
    # The web app saves as `{doc_id}.json`.
    # doc_id in this script will probably be the filename.
    corrected_files = set(os.listdir(corrected_dir))
    
    input_data = {}
    
    json_files = load_ground_truth_files(ground_truth_dir)
    print(f"Found {len(json_files)} Ground Truth files.")
    
    count = 0
    for json_path in json_files:
        filename = os.path.basename(json_path)
        doc_id = filename # unique ID
        
        # Check if already done
        if doc_id in corrected_files:
            continue
            
        tags = parse_quantitative_json(json_path)
        if not tags:
            # print(f"No tags found in {filename}, skipping or treating as empty.")
            pass
            
        # Get text
        text = get_text_content(filename)
        if not text:
            # Attempt to find text in sibling directory? or assume user mapped it?
            # Let's try to remove .json and see.
            # If not found, maybe we can't visualize it?
            # Wait, `web_app.py` NEEDS text.
            # I must verify where the text files are.
            print(f"WARNING: Text file not found for {filename}. Skipping.")
            continue
            
        input_data[doc_id] = {
            "text": text,
            "model_tags": tags,  # Load as SUGGESTIONS
            "real_tags": []      # Empty to force validation
        }
        count += 1
        
    print(f"Prepared {count} documents for validation.")
    print(f"Skipped {len(json_files) - count} documents (already corrected or missing text).")
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(input_data, f, indent=4)
    print(f"Generated {output_file}")

if __name__ == "__main__":
    main()
