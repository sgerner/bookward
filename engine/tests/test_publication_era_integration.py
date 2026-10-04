"""Integration boundaries for the optional, validated era correction."""
from types import SimpleNamespace

import numpy as np

from afterword_engine.ranking import rank_candidates
from afterword_engine.scoring_evidence import READ_FIELDS, CANDIDATE_FIELDS
from afterword_engine.decision_evidence import _runtime_source_lineage as source_lineage


def test_ranker_keeps_unknown_years_identical_and_prevents_history_label_leak(monkeypatch):
    from afterword_engine import publication_era_signal

    def forbidden(*args, **kwargs):
        raise AssertionError("year model must not fit unknown-year or self-rating diagnostics")

    monkeypatch.setattr(publication_era_signal, "fit_publication_era_signal", forbidden)
    reads = [{"id": 1, "title": "A", "author": "X", "rating": 5}]
    vectors = [[1., 0.]]
    candidate = {"id": 2, "title": "B", "author": "Y"}
    assert rank_candidates(reads, vectors, [candidate], vectors) == rank_candidates(
        reads, vectors, [candidate], vectors, publication_era=False
    )
    candidate["first_publication_year"] = 1970
    assert rank_candidates(reads, vectors, [candidate], vectors, exclude_candidate_identity=True) == rank_candidates(
        reads, vectors, [candidate], vectors, exclude_candidate_identity=True, publication_era=False
    )


def test_validated_adjustment_uses_exact_baseline_score_scale_and_is_archived(monkeypatch):
    from afterword_engine import publication_era_signal

    reads = [{"id": 1, "title": "A", "author": "X", "rating": 5}]
    vectors = np.array([[1., 0.]])
    candidate = {"id": 2, "title": "B", "author": "Y", "first_publication_year": 1970,
                 "catalog_confidence": .9}
    baseline = rank_candidates(reads, vectors, [candidate], vectors, publication_era=False)[0]

    def fit(history, history_vectors, callback):
        # Re-entering the baseline must not fit another era model.
        scores = callback(history, history_vectors, [candidate], vectors)
        assert scores == [baseline["score"]]
        return SimpleNamespace(adjustment=lambda year: 2. if year == 1970 else 0.,
                               diagnostics={"enabled": True, "reason": "validated"})

    monkeypatch.setattr(publication_era_signal, "fit_publication_era_signal", fit)
    result = rank_candidates(reads, vectors, [candidate], vectors)[0]
    assert result["score"] == round(baseline["score"] + 2, 1)
    assert result["publication_era"]["score_adjustment"] == round(2, 4)
    assert "held-out ratings" in " ".join(result["explanation"])
    assert "first_publication_year" in READ_FIELDS
    assert "first_publication_year" in CANDIDATE_FIELDS
    lineage = source_lineage()
    assert "publication_era_signal.py" in str(lineage)
    assert "publication_year_metadata.py" in str(lineage)


def test_read_metadata_queues_rescore_only_when_verified_year_inputs_change(monkeypatch):
    import asyncio
    from contextlib import nullcontext
    from afterword_engine import main, publication_year_metadata

    current = {"year": None}
    queued = []
    monkeypatch.setattr(main, "private_settings", lambda: {})
    monkeypatch.setattr(main, "rows", lambda *_: [{"id": 1, "rating": 4}])
    monkeypatch.setattr(main, "connect", lambda: nullcontext(None))
    monkeypatch.setattr(main, "enqueue_job", lambda kind, **kwargs: queued.append(kind) or "new-job")

    def attach(db, reads, candidates):
        for item in reads:
            item["first_publication_year"] = current["year"]

    async def refresh():
        current["year"] = 1975
        return {"updated": 1, "remaining": 0}

    monkeypatch.setattr(publication_year_metadata, "attach_publication_years", attach)
    monkeypatch.setattr(main, "refresh_read_metadata", refresh)
    assert asyncio.run(main.handle_job("read_metadata"))["score_job_id"] == "new-job"
    assert queued == ["score"]
    queued.clear()
    assert asyncio.run(main.handle_job("read_metadata"))["score_job_id"] is None
    assert queued == []


def test_real_fit_affine_conversion_matches_final_rounding_and_clipping(monkeypatch):
    from datetime import date, timedelta
    from afterword_engine import publication_era_signal
    from afterword_engine.publication_era_signal import PublicationEraFit, _Calibration, _EraModel

    years = np.arange(1960, 1976, dtype=float)
    model = _EraModel(years, np.full(16, .1), tuple(f'Author {i}' for i in range(16)),
                      tuple(date(2020, 1, 1) + timedelta(days=i) for i in range(16)))
    fitted = PublicationEraFit({'status': 'activated'}, model, _Calibration(0., .2), True)
    assert fitted.adjustment(1970) == .5  # residual stars / fitted stars per score point
    monkeypatch.setattr(publication_era_signal, 'fit_publication_era_signal', lambda *_: fitted)
    reads = [{'id': 1, 'title': 'Read', 'author': 'A', 'rating': 5}]
    candidates = [{'id': 2, 'title': 'Ordinary', 'author': 'B', 'first_publication_year': 1970},
                  {'id': 3, 'title': 'Upper bound', 'author': 'B', 'first_publication_year': 1970,
                   'source_weight': 20}]
    rv, cv = [[1., 0.]], [[.5, .8], [1., 0.]]
    base = rank_candidates(reads, rv, candidates, cv, publication_era=False)
    corrected = rank_candidates(reads, rv, candidates, cv)
    assert [item['score'] for item in corrected] == [round(min(100., item['score'] + .5), 1) for item in base]
    assert corrected[-1]['score'] == 100.
