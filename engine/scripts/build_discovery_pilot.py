#!/usr/bin/env python3
"""Freeze four discovery policies and create a local blinded human-interest pilot.

No labels are imputed. Raw identities, source assignments and user judgments stay
outside Git. Sampling probabilities distinguish random coverage from oversampled
policy disagreements. Interest is not enjoyment after reading.
"""
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import numpy as np
import httpx
sys.path.insert(0,str(Path(__file__).resolve().parent))
import audit_production_pipeline as audit
import evaluate_typed_retrieval as typed
from prepare_discovery_judgments import write
from afterword_engine.main import recommendation_list
from afterword_engine.interaction_personalization import load_interaction_events, personalize_recommendations
from afterword_engine.discovery_slate import diversify_discovery_slate
from afterword_engine import ingestion as book_ingestion
from afterword_engine.ranking import rank_candidates
from afterword_engine.scoring import document
from afterword_engine.identity import book_identity
from afterword_engine.isbn import isbn_parts


def reserve_alternative(rows, vectors):
    """One fixed slot, bounded sacrifice/displacement; missing vectors mean no swap."""
    rows=[dict(x) for x in rows]
    if len(rows)<9: return rows
    def redundant(item):
        for other in rows[:7]:
            if book_identity('',item.get('author',''))==book_identity('',other.get('author','')):
                return True
            a,b=vectors.get(int(item['id'])),vectors.get(int(other['id']))
            if a is not None and b is not None:
                norm=np.linalg.norm(a)*np.linalg.norm(b)
                if norm and float(np.dot(a,b)/norm)>=.9: return True
        return False
    # Reserve only if eighth slot is redundant. A novel book is not intrinsically better.
    if not redundant(rows[7]): return rows
    for j in range(8,min(11,len(rows))):
        if rows[7]['score']-rows[j]['score']<=2. and not redundant(rows[j]):
            rows[7],rows[j]=rows[j],rows[7]
            break
    return rows


def sample_blind(pool, policies, seed=20261002):
    rng=np.random.default_rng(seed)
    byid={int(x['id']):x for x in pool}
    top_union=set(int(x['id']) for rows in policies.values() for x in rows[:8])
    selected=[]
    # Three strata; shared only if verified identity overlap exists.
    for stratum in ('current_only','typed_only','shared'):
        ids=sorted(k for k,x in byid.items() if x['_stratum']==stratum)
        if not ids: continue
        priority=sorted(set(ids)&top_union)
        priority=priority[:12]
        remaining=[k for k in ids if k not in priority]
        take=min(12,len(remaining))
        sampled=list(map(int,rng.choice(remaining,take,replace=False))) if take else []
        for k in priority:
            selected.append({'id':k,'stratum':stratum,'sampling':'policy_top8','inclusion_probability':1.})
        for k in sampled:
            selected.append({'id':k,'stratum':stratum,'sampling':'random_coverage','inclusion_probability':take/len(remaining)})
    # If policy top-union has fewer than12, don't silently fill it with score-selected books.
    rng.shuffle(selected)
    for index,x in enumerate(selected):
        x['blind_id']=hashlib.sha256(f'{seed}:{x["id"]}'.encode()).hexdigest()[:16]
        x['position']=index+1
    return selected


async def resolve_card_description(item, work_client, publisher_resolver):
    """Use one source order for every sampled card, independent of policy arm."""

    work_id=typed._work_key(item.get('work_id'))
    evidence={'work_id':work_id,'status':'no_verified_work_id'}
    description=''
    if work_id:
        try:
            payload=await work_client.get_json(work_id+'.json')
            if typed._work_key(payload.get('key'))!=work_id:
                raise ValueError('Work identity mismatch')
            description=typed._description_text(payload.get('description'))
            evidence={'work_id':work_id,'status':'verified_work','provider':'openlibrary',
                      'provider_id':work_id,'kind':'work_description',
                      'source_field':'description',
                      'description_sha256':hashlib.sha256(description.encode()).hexdigest()}
        except Exception as exc:
            evidence={'work_id':work_id,'status':'work_fetch_failed','error_type':type(exc).__name__}
    if description:
        return description[:1800],evidence

    isbn13,isbn10=isbn_parts(item.get('isbn13'),item.get('isbn10'))
    source_url=str(item.get('source_url') or '')
    if (isbn13 or isbn10) and book_ingestion._penguin_random_house_book_url(source_url):
        try:
            publisher=await publisher_resolver(item)
        except Exception as exc:
            evidence={**evidence,'publisher_status':'fetch_failed','publisher_error_type':type(exc).__name__}
            return '',evidence
        returned_isbns=[publisher.get('isbn13'),publisher.get('isbn10')]
        expected13,expected10=isbn_parts(isbn13,isbn10)
        expected_provider_id=f"https://{urlparse(source_url).netloc}{urlparse(source_url).path}"
        exact_isbn=book_ingestion._returned_identifiers_match_read(returned_isbns,expected13,expected10)
        publisher_description=str(publisher.get('description') or '').strip()
        if (
            publisher.get('provider')=='penguinrandomhouse'
            and publisher.get('provider_id')==expected_provider_id
            and exact_isbn
            and publisher_description
        ):
            evidence={
                'work_id':work_id,'status':'verified_publisher_product',
                'provider':publisher['provider'],'provider_id':publisher['provider_id'],
                'kind':publisher.get('kind') or 'publisher_product',
                'source_field':publisher.get('source_field') or 'description',
                'isbn13':isbn13,'isbn10':isbn10,
                'description_sha256':hashlib.sha256(publisher_description.encode()).hexdigest(),
            }
            return publisher_description[:1800],evidence
        evidence={**evidence,'publisher_status':'identity_or_field_unverified'}
    return '',evidence


def _blind_card_payload(cards):
    """Serialize only visible card fields, with script-safe JSON escaping.

    Source identity and resolver evidence stay in private artifacts, not in
    the blinded HTML where a judge could inspect them through page source.
    """
    visible_fields=('blind_id','title','author','description')
    visible=[{key:card.get(key,'') for key in visible_fields} for card in cards]
    return json.dumps(visible,ensure_ascii=False).replace('<','\\u003c')


async def build(snapshot_path,discovery_dir):
    out=Path(discovery_dir)
    if (out/'pilot-manifest-private.json').exists():
        raise ValueError('Pilot already frozen; do not regenerate after judgments')
    snapshot=json.loads(Path(snapshot_path).read_text())
    raw=json.loads((out/'capture-private.json').read_text())
    capture=typed._capture_from_private_record(raw['captures']['typed'])
    con=audit._fixture_connection(snapshot)
    current=recommendation_list(con,status='recommended',limit=None)
    events=load_interaction_events(con)
    vectors,_=audit._cache_items(current,events,con,corrected_event_key=True)
    now=audit._as_datetime(snapshot['exported_at_utc'])
    quality={int(x['candidate_id']):x for x in snapshot['candidate_quality']}
    for x in current:
        x['_stratum']='current_only'
        x['work_id']=quality.get(int(x['id']),{}).get('work_id') or ''
    typed_rows=[]
    for i,work in enumerate(capture.eligible.values()):
        row=work.candidate_row(enriched=True)
        row['id']=1000000000+i
        row['status']='recommended'
        row['source_weight']=.25 # Fixed comparable OpenLibrary association priority; no tuning.
        row['_stratum']='typed_only'
        row['catalog_confidence']=1.
        match=next((x for x in current if typed.match_method(row,x)),None)
        if match:
            match['_stratum']='shared'
        else: typed_rows.append(row)
    texts=[document(x) for x in typed_rows]
    encoded=[]
    async with httpx.AsyncClient(timeout=180) as client:
        for start in range(0,len(texts),16):
            response=await client.post('http://127.0.0.1:11434/api/embed',json={'model':'qwen3-embedding:4b','input':texts[start:start+16]})
            response.raise_for_status()
            batch=response.json()['embeddings']
            if len(batch)!=len(texts[start:start+16]): raise ValueError('Embedding count mismatch')
            encoded.extend(batch)
    matrix=np.asarray(encoded,dtype=np.float32)
    if not np.isfinite(matrix).all(): raise ValueError('Nonfinite embeddings')
    reads=[x for x in snapshot['reads'] if x.get('rating') is not None and 1<=float(x['rating'])<=5]
    history=[]
    for x in reads:
        vector,state,_=audit._vector_record(snapshot,'read',x,backend='ollama',model='qwen3-embedding:4b')
        if state!='fresh': raise ValueError('Incomplete execution-matched read history')
        history.append(vector)
    ranked=rank_candidates(reads,np.asarray(history),typed_rows,matrix)
    # Scorer retains IDs but not all pool annotations in every implementation.
    scored={int(x['id']):x for x in ranked}
    typed_rows=[{**x,**scored[int(x['id'])]} for x in typed_rows]
    vectors.update({int(x['id']):v for x,v in zip(typed_rows,matrix)})
    pool=current+typed_rows
    policies={}
    for label,rows in [('current',current),('union',pool)]:
        personalized,_=personalize_recommendations(rows,events,now=now,interaction_vectors={int(x['candidate_id']):vectors[int(x['candidate_id'])] for x in events if int(x['candidate_id']) in vectors},candidate_vectors=vectors)
        slate,_=diversify_discovery_slate(personalized,vectors)
        policies[label+'_current_slate']=slate
        policies[label+'_reserved_slate']=reserve_alternative(personalized,vectors)
    sample=sample_blind(pool,policies)
    byid={int(x['id']):x for x in pool}
    client=typed.OpenLibraryProbeClient(request_budget=len(sample),rate_limiter=typed.RateLimiter(1.))
    cards=[]
    # One shared evidence resolver handles cards drawn from every policy arm:
    # canonical Open Library work description first, then an exact-ISBN
    # publisher product description only for a direct allowlisted product URL.
    publisher_resolution={'attempted':0,'verified':0}
    async with book_ingestion._source_client() as publisher_client:
        async def publisher_resolver(item):
            publisher_resolution['attempted']+=1
            result=await book_ingestion.resolve_penguin_random_house_product_description(
                item,client=publisher_client
            )
            publisher_resolution['verified']+=bool(result.get('description'))
            return result

        for sampled in sample:
            item=byid[sampled['id']]
            description,evidence=await resolve_card_description(item,client,publisher_resolver)
            sampled['description_evidence']=evidence
            cards.append({'blind_id':sampled['blind_id'],'title':item['title'],'author':item['author'],
                          'description':description[:1800] or 'No verified synopsis available.',
                          'description_evidence':evidence})
    manifest={'version':1,'frozen_at':datetime.now(timezone.utc).isoformat(),'snapshot_sha256':hashlib.sha256(Path(snapshot_path).read_bytes()).hexdigest(),
              'capture_sha256':hashlib.sha256((out/'capture-private.json').read_bytes()).hexdigest(),
              'candidate_vectors':'Qwen4b matching snapshot dimension; source weight .25 for new typed works',
              'policy_rules':{'reserve_slot':8,'max_displacement':3,'max_score_sacrifice':2.,'requires_redundant_slot':True},
              'policies':{k:[int(x['id']) for x in v] for k,v in policies.items()},'sample':sample,
              'openlibrary_work_description_requests':client.aggregate(),
              'publisher_description_request_budget':len(sample),
              'shared_card_description_policy':'One resolver for every arm: Open Library work description first; then exact-ISBN Penguin Random House product description for direct allowlisted publisher links; no title/model generated text.',
              'publisher_description_requests':publisher_resolution,
              'judgments':'pending human input; no generated labels',
              'limitation':'Current catalog pilot. Source failures retained; pool size and novelty not utility proof.'}
    write(out/'pilot-manifest-private.json',manifest)
    write(out/'pilot-cards-private.json',cards)
    payload=_blind_card_payload(cards)
    html='''<!doctype html><html><meta charset="utf-8"><title>Book recommendation judgments</title><style>body{max-width:780px;margin:35px auto;font:18px system-ui;background:#faf8f4;color:#222}article{padding:24px;border:1px solid #ccc;border-radius:12px;background:white;margin:20px 0}button{padding:12px;margin:6px;border-radius:6px;cursor:pointer}button.selected{background:#174e65;color:white}p{white-space:pre-wrap;line-height:1.5}header{position:sticky;top:0;background:#faf8f4;padding:10px}small{color:#555}</style><header><h1>Which books interest you?</h1><p>Judge interest in reading each book. “Unsure / not now” stays unknown. This measures interest, not enjoyment after reading. Source and ranking labels are hidden.</p><button id="export">Export judgments</button><span id="progress"></span></header><main id="cards"></main><script>const cards=PAYLOAD;const key='bookward-blind-interest-20261002';let answers=JSON.parse(localStorage.getItem(key)||'{}');const root=document.getElementById('cards');function update(){document.getElementById('progress').textContent=Object.keys(answers).length+' / '+cards.length+' answered';}for(const [i,c] of cards.entries()){const a=document.createElement('article');const h=document.createElement('h2');h.textContent=(i+1)+'. '+c.title;a.append(h);const author=document.createElement('small');author.textContent=c.author;a.append(author);const p=document.createElement('p');p.textContent=c.description;a.append(p);for(const [value,label] of [['interested','Interested'],['not_interested','Not interested'],['unsure','Unsure / not now'],['already_read','Already read']]){const b=document.createElement('button');b.textContent=label;b.classList.toggle('selected',answers[c.blind_id]===value);b.onclick=()=>{answers[c.blind_id]=value;localStorage.setItem(key,JSON.stringify(answers));a.querySelectorAll('button').forEach(x=>x.classList.remove('selected'));b.classList.add('selected');update();};a.append(b);}root.append(a);}update();document.getElementById('export').onclick=()=>{const data={version:1,endpoint:'interest',answered_at:new Date().toISOString(),judgments:Object.entries(answers).map(([blind_id,judgment])=>({blind_id,judgment}))};const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));a.download='bookward-interest-judgments.json';a.click();URL.revokeObjectURL(a.href);};</script></html>'''.replace('PAYLOAD',payload)
    path=out/'blind-interest-pilot.html';path.write_text(html);os.chmod(path,0o600)
    np.savez_compressed(out/'typed-vectors-private.npz',candidate_ids=np.asarray([x['id'] for x in typed_rows]),vectors=matrix)
    os.chmod(out/'typed-vectors-private.npz',0o600)
    write(out/'policy-pools-private.json',{'pool':pool,'policies':policies})
    print(json.dumps({'sample_n':len(sample),'strata':{k:sum(x['stratum']==k for x in sample) for k in ('current_only','typed_only','shared')},'current_n':len(current),'typed_new_n':len(typed_rows),'policy_top8_overlap':{k:len(set(vv['id'] for vv in v[:8])&set(vv['id'] for vv in policies['current_current_slate'][:8])) for k,v in policies.items()},'description_requests':client.aggregate()}))

if __name__=='__main__': asyncio.run(build(*sys.argv[1:]))
