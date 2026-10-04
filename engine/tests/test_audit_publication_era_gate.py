import base64
import importlib.util
from pathlib import Path

import numpy as np
import pytest

from afterword_engine.embeddings import content_hash
from afterword_engine.scoring import document

spec = importlib.util.spec_from_file_location('era_gate_audit', Path(__file__).parents[1] / 'scripts/audit_publication_era_gate.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def capture():
    read = {'id': 1, 'title': 'Read', 'author': 'One', 'rating': 4, 'read_at': '2024-01-01'}
    candidate = {'id': 2, 'title': 'Candidate', 'author': 'Two', 'status': 'new',
                 'source_id': 7, 'quality_status': 'accepted'}
    vector = np.array([1., 0.], dtype=np.float32)
    embeddings = [{'entity_type': kind, 'entity_id': item['id'], 'backend': 'test', 'model': 'test',
                   'vector': base64.b64encode(vector.tobytes()).decode(), 'dimensions': 2,
                   'content_hash': content_hash(document(item))}
                  for kind, item in [('read', read), ('candidate', candidate)]]
    return {'reads': [read], 'candidates': [candidate], 'sources': [{'id': 7, 'enabled': 1}],
            'settings': {'embedding_backend': 'test', 'embedding_model': 'test'}, 'embeddings': embeddings}


def test_private_audit_keeps_unknown_year_neutral_and_emits_aggregates():
    result = audit.audit(capture(), {})
    assert result['rated_reads'] == result['eligible_candidates'] == 1
    assert result['verified_read_years'] == result['verified_candidate_years'] == 0
    assert result['changed_candidate_scores'] == result['maximum_absolute_score_change'] == 0
    assert 'Candidate' not in str(result)
    assert 'Read' not in str(result)


def test_private_audit_rejects_vectors_for_different_scoring_documents():
    data = capture()
    data['reads'][0]['title'] = 'Changed after embedding'
    with pytest.raises(ValueError, match='captured scoring document'):
        audit.audit(data, {})
