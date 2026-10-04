#!/usr/bin/env python3
"""Replay the era gate on a private scoring capture; emit aggregate results only.

The optional provenance capture is the field-ledger projection retained by the
publication-era audit. Its explicitly named publication_payload is restored to
its original ledger wrapper before the production metadata loader validates it.
No source lookup, embeddings, database writes, or production changes occur.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from afterword_engine.decision_evidence import _runtime_source_lineage
from afterword_engine.embeddings import content_hash
from afterword_engine.identity import book_identity_match_index, book_row_identity_match_keys
from afterword_engine.publication_year_metadata import attach_publication_years
from afterword_engine.ranking import rank_candidates
from afterword_engine.scoring import document


def audit(capture: dict, provenance: dict) -> dict:
    reads = [dict(item) for item in capture['reads'] if 1 <= float(item.get('rating') or 0) <= 5]
    excluded = book_identity_match_index(capture['reads'])
    enabled = {int(item['id']) for item in capture['sources'] if item.get('enabled')}
    candidates = [dict(item) for item in capture['candidates']
                  if item.get('status') in {'new', 'recommended'}
                  and item.get('quality_status') == 'accepted'
                  and int(item['source_id']) in enabled
                  and not book_row_identity_match_keys(item) & excluded]
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE read_metadata(read_id INTEGER,verified_work_id TEXT,identity_hash TEXT,
                                   release_date TEXT,metadata_provenance TEXT);
        CREATE TABLE candidate_quality(candidate_id INTEGER,quality_status TEXT,provider TEXT,work_id TEXT);
        CREATE TABLE metadata_field_provenance(entity_type TEXT,entity_id INTEGER,field TEXT,
                                             provider TEXT,provider_id TEXT,source_payload TEXT);
    ''')
    for item in capture.get('read_metadata', []):
        values = [item.get(key) for key in ('read_id', 'verified_work_id', 'identity_hash', 'release_date', 'metadata_provenance')]
        if isinstance(values[-1], dict):
            values[-1] = json.dumps(values[-1])
        db.execute('INSERT INTO read_metadata VALUES(?,?,?,?,?)', values)
    for item in capture.get('candidate_quality', []):
        db.execute('INSERT INTO candidate_quality VALUES(?,?,?,?)',
                   [item.get(key) for key in ('candidate_id', 'quality_status', 'provider', 'work_id')])
    for kind in ('read', 'candidate'):
        for item in provenance.get(f'{kind}_field_provenance', []):
            wrapper = item.get('source_payload') or {}
            if isinstance(wrapper, str):
                wrapper = json.loads(wrapper)
            wrapper = dict(wrapper)
            if 'payload' not in wrapper and 'publication_payload' in wrapper:
                wrapper['payload'] = wrapper.pop('publication_payload')
            db.execute('INSERT INTO metadata_field_provenance VALUES(?,?,?,?,?,?)',
                       (kind, item['entity_id'], item['field'], item['provider'], item['provider_id'], json.dumps(wrapper)))
    attach_publication_years(db, reads, candidates)
    db.close()
    backend = capture['settings']['embedding_backend']
    model = capture['settings']['embedding_model']
    vectors = {(item['entity_type'], int(item['entity_id'])): item for item in capture['embeddings']
               if item['backend'] == backend and item['model'] == model}

    def exact_vectors(kind, items):
        result = []
        for item in items:
            saved = vectors[(kind, int(item['id']))]
            if saved['content_hash'] != content_hash(document(item)):
                raise ValueError(f'{kind} embedding does not match the captured scoring document')
            vector = np.frombuffer(base64.b64decode(saved['vector'], validate=True), dtype=np.float32)
            if len(vector) != saved['dimensions'] or not np.isfinite(vector).all():
                raise ValueError('invalid captured vector')
            result.append(vector)
        return result

    rv, cv = exact_vectors('read', reads), exact_vectors('candidate', candidates)
    baseline = rank_candidates(reads, rv, candidates, cv, publication_era=False)
    started = time.perf_counter()
    corrected = rank_candidates(reads, rv, candidates, cv)
    elapsed = time.perf_counter() - started
    deltas = np.asarray([new['score'] - old['score'] for old, new in zip(baseline, corrected)])
    diagnostics = next((item['publication_era'] for item in corrected if 'publication_era' in item), {})
    diagnostics = {key: value for key, value in diagnostics.items() if key != 'score_adjustment'}
    return {'rated_reads': len(reads), 'verified_read_years': sum(item['first_publication_year'] is not None for item in reads),
            'eligible_candidates': len(candidates), 'verified_candidate_years': sum(item['first_publication_year'] is not None for item in candidates),
            'changed_candidate_scores': int(np.count_nonzero(deltas)),
            'maximum_absolute_score_change': float(np.max(np.abs(deltas))) if len(deltas) else 0.,
            'ranking_seconds': round(elapsed, 3), 'profile_gate': diagnostics,
            'scope': 'Captured base ranking only; no fresh interest outcomes or complete serving-slate replay.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', type=Path, required=True)
    parser.add_argument('--provenance', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = audit(json.loads(args.capture.read_text()), json.loads(args.provenance.read_text()))
    result['runtime_lineage'] = _runtime_source_lineage()
    result['input_sha256'] = {key: hashlib.sha256(path.read_bytes()).hexdigest()
                              for key, path in [('capture', args.capture), ('provenance', args.provenance)]}
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    args.output.chmod(0o600)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
