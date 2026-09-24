from afterword_engine.scoring import document
from afterword_engine.subjects import normalize_subjects


def test_subjects_drop_catalog_artifacts_and_deduplicate_normalized_labels():
    assert normalize_subjects(
        [
            "Self-confidence",
            " self confidence ",
            "89.70 international relations: general",
            "nyt:paperback_advice=2007-07-28",
            "New York Times bestseller",
            "Open Library Staff Picks",
            "Confiance en soi",
        ]
    ) == [
        "Self-confidence",
        "international relations: general",
        "Confiance en soi",
    ]


def test_subjects_accept_json_and_schema_values_with_a_bounded_label_count():
    assert normalize_subjects(
        '["Fiction", {"name":"Historical fiction"}, "Fantasy", "Mystery", "Romance"]',
        limit=3,
    ) == ["Fiction", "Historical fiction", "Fantasy"]
    assert normalize_subjects("not valid json") == ["not valid json"]


def test_embedding_document_uses_clean_subject_text_not_raw_catalog_fields():
    text = document(
        {
            "title": "A Book",
            "author": "A Writer",
            "description": "A concise description.",
            "genres": '["Fiction", "nyt:paperback_advice=2007-07-28", "89.70 Politics"]',
        }
    )
    assert text == "A Book A Writer A concise description. Subjects: Fiction; Politics"
    assert "2007-07-28" not in text


def test_read_embedding_document_preserves_historical_cache_representation():
    assert document({"title": "A read", "author": "Writer", "rating": 5}) == "A read Writer [] "
