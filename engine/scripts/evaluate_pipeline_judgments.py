#!/usr/bin/env python3
"""Descriptive audit-arm replay on the partial blinded pilot judgment sample.

The original five audit arms were frozen before judgments. The source+metadata
combination is an explicitly exploratory sensitivity; no winner is promoted.
"""
import json,sys,os
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parent))
import audit_production_pipeline as audit
import evaluate_next_five_combinations as metric
from afterword_engine.main import recommendation_list
from afterword_engine.interaction_personalization import load_interaction_events
from afterword_engine.ranking import rank_candidates


def run(snapshot_path,directory):
 root=Path(directory);snapshot=json.loads(Path(snapshot_path).read_text())
 manifest=json.loads((root/'pilot-manifest-private.json').read_text());answers=json.loads((root/'judgments-original-private.json').read_text())
 blind={x['blind_id']:x for x in manifest['sample']};labels={blind[x['blind_id']]['id']:x['judgment'] for x in answers['judgments']}
 con=audit._fixture_connection(snapshot);base=recommendation_list(con,status='recommended',limit=None)
 events=load_interaction_events(con);now=audit._as_datetime(snapshot['exported_at_utc'])
 reads=[dict(x) for x in con.execute('SELECT * FROM reads WHERE rating BETWEEN 1 AND 5 ORDER BY id')]
 active={int(x['id']):x for x in audit._score_eligible_candidates(con)}
 rows=[active[int(x['id'])] for x in base]
 rv=[audit._vector_record(snapshot,'read',x,backend='ollama',model='qwen3-embedding:4b')[0] for x in reads]
 cv=[audit._vector_record(snapshot,'candidate',x,backend='ollama',model='qwen3-embedding:4b')[0] for x in rows]
 if any(x is None for x in rv+cv):raise ValueError('Missing aligned vectors')
 arms={};private={}
 for name in ('current','source_neutral','metadata_neutral','interaction_off','slate_off','source_and_metadata_neutral'):
  inputs=[dict(x) for x in rows]
  if name in ('source_neutral','source_and_metadata_neutral'):
   for x in inputs:x['source_weight']=1.
  if name in ('metadata_neutral','source_and_metadata_neutral'):
   for x in inputs:x['catalog_confidence']=None
  scored=rank_candidates(reads,rv,inputs,cv)
  ac=audit._fixture_connection(snapshot)
  for x in scored:
   ac.execute('UPDATE candidates SET score=?,explanation=? WHERE id=?',(x['score'],json.dumps(x['explanation']),x['id']))
   ac.execute('UPDATE candidate_quality SET metadata_confidence=? WHERE candidate_id=?',(x['metadata_confidence'],x['id']))
  ordered=recommendation_list(ac,status='recommended',limit=None)
  vectors,_=audit._cache_items(ordered,events,ac,corrected_event_key=True)
  result,_,_=audit._apply_serving_stages(ordered,events,vectors,now=now,runtime=snapshot['runtime'],interaction_enabled=name!='interaction_off',slate_enabled=name!='slate_off')
  private[name]=result
  known=[x for x in result if labels.get(int(x['id'])) in ('interested','not_interested')]
  interest=np.asarray([labels[int(x['id'])]=='interested' for x in known]);scores=np.asarray([x['score'] for x in known])
  top=[]
  for k in (8,20):
   counts={s:sum(labels.get(int(x['id']),'unsampled')==s for x in result[:k]) for s in ('interested','not_interested','unsure','already_read','unsampled')}
   top.append({'k':k,'labels':counts,'confirmed_interested_slots':counts['interested'],'unknown_slots':counts['unsure']+counts['unsampled']})
  arms[name]={'known_interest_n':len(known),'interested_n':int(interest.sum()),'interest_auc_on_selected_sample':metric.auc(interest,scores) if len(known) else None,'slates':top}
  ac.close()
 output={'arms':arms,'limitations':['Partial deliberately stratified sample, interest AUC is descriptive not full-cohort utility','Unknown synopsis responses remain unknown','Already read is an eligibility report, not dislike','Source+metadata combined arm is exploratory after pilot unblinding','No statistical adoption claim']}
 for name,obj in [('pipeline-judgment-aggregate.json',output),('pipeline-arm-pools-private.json',private)]:
  p=root/name;p.write_text(json.dumps(obj,indent=2,sort_keys=True)+'\n');os.chmod(p,0o600)
 print(json.dumps(output,indent=2))
 return output

if __name__=='__main__':run(*sys.argv[1:])
