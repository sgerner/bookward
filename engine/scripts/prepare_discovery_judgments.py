#!/usr/bin/env python3
"""Build a private, blinded interest pilot; never infer judgments from ratings.

Capture mode uses matched GET ceilings for current-list versus typed retrieval.
The complete known library is excluded, including unrated reads.
"""
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parent))
import evaluate_typed_retrieval as typed
import evaluate_ranking as historical
from afterword_engine.ingestion import _read_metadata_identity_hash
from afterword_engine.identity import book_identity, book_identity_matches


def write(path,value):
    path.write_text(json.dumps(value,sort_keys=True,indent=2)+'\n')
    os.chmod(path,0o600)


def complete_reads(corpus):
    metadata={int(x['read_id']):x for x in corpus.get('read_metadata',[])}
    result=[]
    for original in corpus['reads']:
        read=dict(original)
        meta=metadata.get(int(read['id']))
        if meta and meta.get('identity_hash')==_read_metadata_identity_hash(read):
            for key in ('open_library_work_id','isbn','isbn10','isbn13'):
                if meta.get(key) and not read.get(key): read[key]=meta[key]
        result.append(read)
    return result


async def capture(corpus_path,output_dir):
    out=Path(output_dir);out.mkdir(parents=True,exist_ok=True,mode=0o700);os.chmod(out,0o700)
    raw=historical.load_corpus(Path(corpus_path))
    reads=complete_reads(raw)
    records,_,_=historical.prepare(raw,'ollama','qwen3-embedding:4b')
    boundary=datetime.now(timezone.utc).date()
    protocol={'captured_at':datetime.now(timezone.utc).isoformat(),'corpus_sha256':hashlib.sha256(Path(corpus_path).read_bytes()).hexdigest(),
              'arms':['existing_list_extra_budget','typed_current_extra_budget'],'get_ceiling_per_arm':20,
              'max_items_per_get':50,'requests_per_second':1,'retry':False,'seed_policy':'current four',
              'exclude':'complete read library including unrated; quality verified canonical work IDs',
              'judgment_endpoint':'interest only; unsure is unknown, never negative',
              'design':'current pool vs union typed pool, crossed with bounded alternative slate; blinded source/model labels',
              'sampling':'48 slots stratified current-only, typed-only, shared; half top/disagreement, half random coverage where feasible',
              'labels':'human supplied after freezing policies; no synthetic labels'}
    write(out/'protocol-private.json',protocol)
    limiter=typed.RateLimiter(1.)
    current_client=typed.OpenLibraryProbeClient(request_budget=20,rate_limiter=limiter)
    typed_client=typed.OpenLibraryProbeClient(request_budget=20,rate_limiter=limiter)
    current=await typed.capture_list_baseline(name='list_current',prefix_reads=reads,boundary_day=boundary,request_client=current_client)
    extra=await typed.capture_typed_graph(name='typed_current',seed_policy='current',prefix_records=records,prefix_reads=reads,boundary_day=boundary,request_client=typed_client)
    payload={'protocol':protocol,'captures':{'current':typed._private_capture_record(current),'typed':typed._private_capture_record(extra)}}
    write(out/'capture-private.json',payload)
    print(json.dumps({'current':{'eligible':len(current.eligible),'requests':current_client.aggregate()},'typed':{'eligible':len(extra.eligible),'requests':typed_client.aggregate()}}))


if __name__=='__main__':
    asyncio.run(capture(*sys.argv[1:]))
