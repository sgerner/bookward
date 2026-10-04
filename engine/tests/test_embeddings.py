import pytest

from afterword_engine.embeddings import _ordered_openai_embeddings


def test_openai_embeddings_are_restored_to_request_order():
    response = {
        "data": [
            {"index": 1, "embedding": [0.0, 1.0]},
            {"index": 0, "embedding": [1.0, 0.0]},
        ]
    }
    assert _ordered_openai_embeddings(response, 2) == [[1.0, 0.0], [0.0, 1.0]]


def test_openai_embeddings_without_indexes_are_rejected_as_ambiguous():
    response = {"data": [{"embedding": [1.0]}, {"embedding": [2.0]}]}
    with pytest.raises(ValueError, match="omitted vector indexes"):
        _ordered_openai_embeddings(response, 2)


@pytest.mark.parametrize(
    ("response", "expected_count", "message"),
    [
        ({"data": [{"index": 0, "embedding": [1]}, {"index": 0, "embedding": [2]}]}, 2, "indexes"),
        ({"data": [{"index": 0, "embedding": [1]}, {"embedding": [2]}]}, 2, "incomplete"),
        ({"data": [{"index": 0, "embedding": [1]}]}, 2, "number"),
        ({"data": [{"index": 0}]}, 1, "malformed vectors"),
    ],
)
def test_openai_embeddings_reject_ambiguous_or_malformed_responses(
    response, expected_count, message
):
    with pytest.raises(ValueError, match=message):
        _ordered_openai_embeddings(response, expected_count)
