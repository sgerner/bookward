import asyncio
import json
import sys
from pathlib import Path
import numpy as np
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
import analyze_discovery_judgments as analysis
import build_discovery_pilot as pilot


def test_unknown_and_already_read_are_never_negative_interest():
    items=[{'blind_id':str(i)} for i in range(4)]
    result=analysis.summary(items,{'0':'interested','1':'not_interested','2':'unsure','3':'already_read'})
    assert result['known_interest_n']==2
    assert result['interested_fraction_of_known']==.5
    assert result['interest_bounds_among_not_already_read']==[1/3,2/3]


def test_judgment_join_rejects_unknown_duplicate_and_invalid_labels():
    manifest={'sample':[{'blind_id':'a'}]}
    for labels in ([{'blind_id':'b','judgment':'interested'}],
                   [{'blind_id':'a','judgment':'bad'}],
                   [{'blind_id':'a','judgment':'unsure'}]*2):
        with pytest.raises(ValueError):analysis.validate(manifest,{'judgments':labels})


def test_reserved_slot_obeys_nonredundancy_score_and_displacement_bounds():
    rows=[{'id':i,'author':f'Author {i}','score':60.-i/10} for i in range(11)]
    rows[7]['author']=rows[0]['author']
    result=pilot.reserve_alternative(rows,{})
    assert result[7]['id']==8 and result[8]['id']==7
    assert rows[7]['id']==7
    rows[8]['score']=50;rows[9]['score']=49;rows[10]['score']=48
    assert pilot.reserve_alternative(rows,{})==rows


def test_coverage_sampling_records_population_probabilities_and_is_repeatable():
    rows=[{'id':i,'_stratum':'typed_only'} for i in range(100)]
    policies={'a':rows}
    first=pilot.sample_blind(rows,policies)
    assert first==pilot.sample_blind(rows,policies)
    priority=[x for x in first if x['sampling']=='policy_top8']
    coverage=[x for x in first if x['sampling']=='random_coverage']
    assert len(priority)==8 and len(coverage)==12
    assert all(x['inclusion_probability']==12/92 for x in coverage)
    assert len({x['id'] for x in first})==20


def test_blind_html_payload_hides_provenance_and_escapes_untrusted_text():
    card={
        'blind_id':'opaque-card-id',
        'title':'Title </script><script>alert(1)</script>',
        'author':'Author & Co.',
        'description':'Synopsis with </script> text.',
        'description_evidence':{
            'provider':'penguinrandomhouse',
            'provider_id':'https://www.penguinrandomhouse.com/books/123/private-work',
            'isbn13':'9780306406157',
            'work_id':'/works/OL123W',
        },
    }
    payload=pilot._blind_card_payload([card])
    decoded=json.loads(payload)

    assert decoded==[{
        'blind_id':'opaque-card-id',
        'title':card['title'],
        'author':card['author'],
        'description':card['description'],
    }]
    assert '</script>' not in payload.casefold()
    assert r'\u003c/script>' in payload
    for private_value in ('penguinrandomhouse','private-work','9780306406157','OL123W'):
        assert private_value not in payload


def test_shared_card_resolver_falls_back_to_exact_isbn_publisher_provenance():
    item={
        'title':'Source Work','author':'A Writer','work_id':'/works/OL123W',
        'source_url':'https://www.penguinrandomhouse.com/books/123/source-work-by-a-writer',
        'isbn13':'9780306406157',
    }

    class WorkClient:
        async def get_json(self,path):
            return {'key':'/works/OL123W','description':''}

    async def publisher_lookup(_item):
        return {
            'description':'Publisher product description.',
            'isbn13':'9780306406157','isbn10':'',
            'provider':'penguinrandomhouse',
            'provider_id':'https://www.penguinrandomhouse.com/books/123/source-work-by-a-writer',
            'kind':'publisher_product','source_field':'description',
        }

    description,evidence=asyncio.run(pilot.resolve_card_description(item,WorkClient(),publisher_lookup))

    assert description=='Publisher product description.'
    assert evidence['status']=='verified_publisher_product'
    assert evidence['provider']=='penguinrandomhouse'
    assert evidence['isbn13']==item['isbn13']


def test_shared_card_resolver_rejects_publisher_isbn_mismatch():
    item={
        'title':'Source Work','author':'A Writer','work_id':'/works/OL123W',
        'source_url':'https://www.penguinrandomhouse.com/books/123/source-work-by-a-writer',
        'isbn13':'9780306406157',
    }

    class WorkClient:
        async def get_json(self,path):
            return {'key':'/works/OL123W','description':''}

    async def publisher_lookup(_item):
        return {
            'description':'Wrong-edition description.',
            'isbn13':'9780140328721','isbn10':'',
            'provider':'penguinrandomhouse',
            'provider_id':'https://www.penguinrandomhouse.com/books/123/source-work-by-a-writer',
            'kind':'publisher_product','source_field':'description',
        }

    description,evidence=asyncio.run(pilot.resolve_card_description(item,WorkClient(),publisher_lookup))

    assert description==''
    assert evidence['publisher_status']=='identity_or_field_unverified'
