#!/usr/bin/env python3
"""Evaluate frozen discovery policies with actual blinded human interest labels.

Unknown and already-read labels never become negatives. Oversampled policy
items and random coverage are reported separately, with inclusion weighting
only for known-answer coverage. Bounds include unknowns without assuming MAR.
"""
import json
import os
import sys
from pathlib import Path
from collections import Counter
import numpy as np

LABELS={'interested','not_interested','unsure','already_read'}

def validate(manifest,answers):
    expected={x['blind_id']:x for x in manifest['sample']}
    if len(expected)!=len(manifest['sample']):raise ValueError('Duplicate manifest IDs')
    observed={}
    for x in answers['judgments']:
        key=x['blind_id'];value=x['judgment']
        if key not in expected or key in observed or value not in LABELS:
            raise ValueError('Unknown/duplicate ID or invalid judgment')
        observed[key]=value
    return expected,observed


def summary(items,labels):
    counts=Counter(labels.get(x['blind_id'],'unanswered') for x in items)
    known=counts['interested']+counts['not_interested']
    # Already read means ineligible, not an interest failure. Unknowns bound utility.
    eligible=len(items)-counts['already_read']
    return {'n':len(items),'labels':dict(counts),'known_interest_n':known,
            'interested_fraction_of_known':counts['interested']/known if known else None,
            'interest_bounds_among_not_already_read':[counts['interested']/eligible,
             (counts['interested']+counts['unsure']+counts['unanswered'])/eligible] if eligible else None}


def analyze(directory,answers_path):
    root=Path(directory)
    manifest=json.loads((root/'pilot-manifest-private.json').read_text())
    answers=json.loads(Path(answers_path).read_text())
    cards={x['blind_id']:x for x in json.loads((root/'pilot-cards-private.json').read_text())}
    expected,observed=validate(manifest,answers)
    sample=list(expected.values())
    byid={x['id']:x for x in sample}
    report={'answered_at':answers.get('answered_at'),'overall':summary(sample,observed),'strata':{},'policies':{},
            'endpoint':'Blinded interest; not reading enjoyment',
            'limitations':['Single reader and one frozen current-catalog pilot; not independent readers',
             'Coverage sample has 12 random rows per populated stratum; top cases are oversampled',
             'Missing synopses cause informative unknown judgments, not negative labels',
             'Interest labels can evaluate frozen policies; using them for fitting requires fresh confirmation',
             'Matched GET ceilings do not repair current-list provider failure; extra pool yield is not preference lift']}
    for stratum in ('current_only','typed_only','shared'):
        items=[x for x in sample if x['stratum']==stratum]
        report['strata'][stratum]={'overall':summary(items,observed)}
        for design in ('policy_top8','random_coverage'):
            subset=[x for x in items if x['sampling']==design]
            report['strata'][stratum][design]=summary(subset,observed)
        weighted_known=weighted_interest=weighted_eligible=weighted_unknown=0.
        for x in items:
            w=1/float(x['inclusion_probability']);label=observed.get(x['blind_id'],'unanswered')
            if label=='already_read':continue
            weighted_eligible+=w
            if label in ('interested','not_interested'):
                weighted_known+=w
                weighted_interest+=w*(label=='interested')
            else:weighted_unknown+=w
        report['strata'][stratum]['inclusion_weighted']={'known_answer_interest_fraction':weighted_interest/weighted_known if weighted_known else None,
            'interest_bounds':[weighted_interest/weighted_eligible,(weighted_interest+weighted_unknown)/weighted_eligible] if weighted_eligible else None,
            'warning':'Known-answer ratio conditions on informative response availability, not full-pool utility'}
    for name,ids in manifest['policies'].items():
        for k in (8,20):
            selected=[byid[x] for x in ids[:k] if x in byid]
            result=summary(selected,observed)
            result['slate_slots']=min(k,len(ids));result['sampled_slots']=len(selected)
            result['unsampled_slots']=result['slate_slots']-len(selected)
            count=result['labels'];eligible=result['slate_slots']-count.get('already_read',0)
            result['full_slate_interest_bounds']=[count.get('interested',0)/eligible,
                (count.get('interested',0)+count.get('unsure',0)+count.get('unanswered',0)+result['unsampled_slots'])/eligible] if eligible else None
            report['policies'][name+f'_top{k}']=result
    missing=[x for x in sample if cards[x['blind_id']]['description']=='No verified synopsis available.']
    present=[x for x in sample if x not in missing]
    report['synopsis_coverage']={'missing':summary(missing,observed),'present':summary(present,observed),
        'by_stratum':{s:sum(x['stratum']==s for x in missing) for s in ('current_only','typed_only','shared')}}
    baseline=manifest['policies']['current_current_slate'][:8]
    union=manifest['policies']['union_current_slate'][:8]
    report['top8_changes']={'shared_n':len(set(baseline)&set(union)),
        'removed_labels':dict(Counter(observed.get(byid[x]['blind_id'],'unanswered') if x in byid else 'unsampled' for x in baseline if x not in union)),
        'added_labels':dict(Counter(observed.get(byid[x]['blind_id'],'unanswered') if x in byid else 'unsampled' for x in union if x not in baseline))}
    path=root/'judgment-aggregate.json';path.write_text(json.dumps(report,indent=2,sort_keys=True)+'\n');os.chmod(path,0o600)
    print(json.dumps(report,indent=2))
    return report

if __name__=='__main__':analyze(*sys.argv[1:])
