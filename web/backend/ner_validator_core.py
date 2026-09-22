def _get_nested(obj, path):
    """Traverse a nested dict using a '->'-separated path string.

    Example: _get_nested(obj, 'numbers->offset') == obj['numbers']['offset']
    Returns None if any key is missing.
    """
    if not path or obj is None:
        return None
    parts = path.split('->')
    cur = obj
    for part in parts:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


class NERValidatorCore:
    def __init__(self, data, schema=None):
        """Initialize validator with event-based data dictionary.

        Args:
            data (dict): Dictionary with document IDs as keys, each containing:
                - text: str
                - real_events: list of dicts representing events
                - model_events: list of dicts representing events
            schema (dict | None): Schema dict with a 'fields' key.
                Each field entry has:
                  - 'path'  (str): '->'-separated path into each event object
        """
        if not isinstance(data, dict):
            raise ValueError("Input data must be a dictionary")

        self.data = data
        self.schema = schema  # may be None → auto-detect
        self.current_doc_index = 0

        # Initialize tracking
        self.doc_final_events = {} # doc_id: list of validated/accepted events
        self.current_differences = {} # doc_id: list of (real_event, model_event) pairs
        self.doc_model_only = {} # doc_id: list of model-only events auto-flagged for re-annotation
        self.history = [] # List for undo operations: (doc_id, index_in_differences, previous_state)

        self.docs_with_differences = []

        # Prepare docs
        for doc_id, doc in self.data.items():
            diffs = self._find_event_differences(doc_id)
            if diffs:
                self.docs_with_differences.append(doc_id)
                self.current_differences[doc_id] = {'pairs': diffs, 'validated_count': 0, 'decisions': {}}
            else:
                # All events match perfectly, save them directly to final events
                self.doc_final_events[doc_id] = list(doc.get('real_events', []))

    # ------------------------------------------------------------------
    # Schema helpers
    # ------------------------------------------------------------------

    def _schema_fields(self):
        """Return the list of field descriptors from the schema."""
        if self.schema and 'fields' in self.schema:
            return self.schema['fields']
        return []

    @staticmethod
    def _is_span_value(value):
        """Heuristic: a dict with 'text' and at least one offset key looks like a span."""
        return (
            isinstance(value, dict)
            and 'text' in value
            and (value.get('end') is not None or value.get('begin') is not None or value.get('start') is not None)
        )

    def _iter_span_fields(self, event):
        """Yield (path, value) for every span field in an event based on schema."""
        if not event:
            return
        fields = self._schema_fields()
        for fd in fields:
            val = _get_nested(event, fd['path'])
            if val is not None and self._is_span_value(val):
                yield fd['path'], val

    def mark_docs_as_processed(self, processed_ids):
        """Mark documents as processed and remove them from validation queue."""
        self.docs_with_differences = [
            doc_id for doc_id in self.docs_with_differences
            if doc_id not in processed_ids
        ]

        if self.current_doc_index >= len(self.docs_with_differences):
            self.current_doc_index = 0

    def _serialize_event(self, event):
        """Creates a sortable representation of the event to detect differences."""
        if not event:
            return ""
        filtered = {k: v for k, v in event.items() if k != 'event_id'}

        parts = []
        fields = self._schema_fields()

        if not fields:
            return ""

        for fd in fields:
            path = fd['path']
            val = _get_nested(filtered, path)
            if val is None:
                continue
            if self._is_span_value(val):
                begin = val.get('begin') if val.get('begin') is not None else val.get('start')
                end = val.get('end')
                parts.append(f"{path}:{begin}-{end}:{val['text']}")
            else:
                parts.append(f"{path}:{val}")

        return "|".join(parts)

    def _event_bounds(self, event):
        """Get the absolute start and end of all span tags inside an event."""
        starts = []
        ends = []
        for _, val in self._iter_span_fields(event):
            if val.get('end') is not None:
                begin = val.get('begin') if val.get('begin') is not None else val.get('start')
                if begin is not None:
                    starts.append(begin)
                    ends.append(val['end'])

        if not starts:
            return 0, 0
        return min(starts), max(ends)

    def _events_overlap(self, e1, e2):
        s1, e1_end = self._event_bounds(e1)
        s2, e2_end = self._event_bounds(e2)
        return max(s1, s2) <= min(e1_end, e2_end)

    def _find_event_differences(self, doc_id):
        """Find discrepant events and pair overlapping ones.

        Policy for unmatched events:
        - real-only events are auto-kept in the final output
        - model-only events are auto-flagged for a second annotation
          round (recorded, not silently dropped, and not added to the
          corrected layer)
        - only overlapping discrepant pairs are shown in the UI
        """
        doc = self.data[doc_id]
        real_events = doc.get('real_events', [])
        model_events = doc.get('model_events', [])

        # 1. Initialize final events with perfect matches
        real_sigs = {self._serialize_event(e): e for e in real_events}
        model_sigs = {self._serialize_event(e): e for e in model_events}

        common_sigs = set(real_sigs.keys()).intersection(model_sigs.keys())

        final_events = [real_sigs[sig] for sig in common_sigs]
        if doc_id not in self.doc_final_events:
            self.doc_final_events[doc_id] = final_events

        # 2. Extract the differing events
        diff_real = [e for sig, e in real_sigs.items() if sig not in common_sigs]
        diff_model = [e for sig, e in model_sigs.items() if sig not in common_sigs]

        # 3. Match them loosely by overlap to present together
        pairs = []
        used_model = set()

        for re in diff_real:
            matched = False
            for i, me in enumerate(diff_model):
                if i not in used_model and self._events_overlap(re, me):
                    pairs.append((re, me))
                    used_model.add(i)
                    matched = True
                    break

            if not matched:
                # Keep unmatched real events automatically so they do not
                # appear as isolated UI tasks that could later duplicate
                # against unmatched model-only events.
                self.doc_final_events[doc_id].append(re)

        # Auto-flag unmatched model-only events for a second annotation
        # round instead of silently dropping them: a share may be genuine
        # events the reference layer missed rather than model hallucinations.
        # They are recorded but not added to the corrected layer.
        self.doc_model_only[doc_id] = [
            me for i, me in enumerate(diff_model) if i not in used_model
        ]

        return pairs

    def _get_total_progress(self):
        """Get total progress information across all documents."""
        total_pairs = 0
        total_validated = 0

        for doc_id in self.docs_with_differences:
            doc_diff_info = self.current_differences[doc_id]
            total_pairs += len(doc_diff_info['pairs'])
            total_validated += doc_diff_info['validated_count']

        return {
            'total_remaining': total_pairs - total_validated,
            'total_tags': total_pairs,
            'completed': total_validated
        }

    def _create_example(self, doc_id, pair_index):
        """Create an example dictionary for the UI with both real and model events."""
        doc = self.data[doc_id]
        progress = self._get_total_progress()
        pair_info = self.current_differences[doc_id]

        real_ev, model_ev = pair_info['pairs'][pair_index]

        return {
            'doc_id': doc_id,
            'text': doc['text'],
            'real_event': real_ev,
            'model_event': model_ev,
            'pair_index': pair_index,
            'total_remaining': len(pair_info['pairs']) - pair_info['validated_count'],
            'progress': progress
        }

    def get_next_example(self):
        """Get the next discrepant event pair to validate."""
        if self.current_doc_index >= len(self.docs_with_differences):
            return {
                'completed': True,
                'progress': self._get_total_progress(),
                'output_file': None
            }

        doc_id = self.docs_with_differences[self.current_doc_index]
        doc_info = self.current_differences[doc_id]

        if doc_info['validated_count'] >= len(doc_info['pairs']):
            self.current_doc_index += 1
            return self.get_next_example()

        pair_idx = 0
        while str(pair_idx) in doc_info['decisions']:
            pair_idx += 1

        return self._create_example(doc_id, pair_idx)

    def can_undo(self):
        return len(self.history) > 0

    @staticmethod
    def _build_decision_record(accepted_event, flagged_fields=None):
        """Normalize a reviewed pair into a serializable decision record."""
        flagged_fields = sorted(set(flagged_fields or []))
        status = "accepted" if accepted_event is not None else "flagged"
        return {
            "status": status,
            "accepted_event": accepted_event,
            "flagged_fields": flagged_fields,
        }

    @staticmethod
    def _coerce_decision_record(decision):
        """Accept legacy raw decisions and normalize them to decision records."""
        if isinstance(decision, dict) and "status" in decision and "accepted_event" in decision:
            return {
                "status": decision["status"],
                "accepted_event": decision.get("accepted_event"),
                "flagged_fields": sorted(set(decision.get("flagged_fields", []))),
            }
        return NERValidatorCore._build_decision_record(decision)

    def undo_last_validation(self):
        if not self.can_undo():
            return self.get_next_example()

        doc_id, pair_index, decision = self.history.pop()

        doc_info = self.current_differences[doc_id]

        if str(pair_index) in doc_info['decisions']:
            del doc_info['decisions'][str(pair_index)]
            doc_info['validated_count'] -= 1

        self.current_doc_index = self.docs_with_differences.index(doc_id)

        return self._create_example(doc_id, pair_index)

    def submit_validation(self, doc_id, pair_index, accepted_event, flagged_fields=None):
        """Submit validation for a pair of disagreeing events."""
        doc_info = self.current_differences[doc_id]

        decision_record = self._build_decision_record(
            accepted_event,
            flagged_fields=flagged_fields,
        )

        doc_info['decisions'][str(pair_index)] = decision_record
        doc_info['validated_count'] += 1

        self.history.append((doc_id, pair_index, decision_record))

        return self.get_next_example()

    def get_final_tags(self):
        """Assemble the final curated tag/event outputs for all documents."""
        result = {}
        for doc_id, doc in self.data.items():
            result[doc_id] = self.get_doc_results(doc_id)
        return result

    def is_doc_complete(self, doc_id):
        if doc_id not in self.current_differences:
            return True
        doc_info = self.current_differences[doc_id]
        return doc_info['validated_count'] >= len(doc_info['pairs'])

    def get_doc_results(self, doc_id):
        final_events = list(self.doc_final_events.get(doc_id, []))

        if doc_id in self.current_differences:
            doc_info = self.current_differences[doc_id]
            for pair_idx, decision in doc_info['decisions'].items():
                decision_record = self._coerce_decision_record(decision)
                accepted_event = decision_record["accepted_event"]
                if accepted_event is not None:
                    final_events.append(accepted_event)

        def get_start(evt):
            s, e = self._event_bounds(evt)
            return s

        final_events.sort(key=get_start)

        return final_events

    def get_doc_model_only(self, doc_id):
        """Return the model-only events auto-flagged for re-annotation.

        These are events the model produced that no reference record
        overlaps. They are recorded for a second annotation round rather
        than silently dropped, and are not part of the corrected layer.
        """
        return list(self.doc_model_only.get(doc_id, []))

    def get_doc_review_state(self, doc_id):
        """Return per-pair review metadata, including unresolved/flagged pairs."""
        reviewed_pairs = []

        if doc_id in self.current_differences:
            doc_info = self.current_differences[doc_id]
            for pair_idx_str, decision in sorted(
                doc_info["decisions"].items(),
                key=lambda item: int(item[0]),
            ):
                decision_record = self._coerce_decision_record(decision)
                reviewed_pairs.append(
                    {
                        "pair_index": int(pair_idx_str),
                        "status": decision_record["status"],
                        "flagged_fields": decision_record["flagged_fields"],
                    }
                )

        unresolved_pair_count = sum(
            1 for pair in reviewed_pairs if pair["status"] == "flagged"
        )

        return {
            "doc_id": doc_id,
            "reviewed_pair_count": len(reviewed_pairs),
            "unresolved_pair_count": unresolved_pair_count,
            "auto_flagged_model_only_count": len(self.doc_model_only.get(doc_id, [])),
            "reviewed_pairs": reviewed_pairs,
        }
