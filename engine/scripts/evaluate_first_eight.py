#!/usr/bin/env python3
"""Development-only causal screen of a capped top-weighted pairwise objective.

Synthetic pools of eventually read books are not real recommendation slates.
All prior labels were inspected; no result is independent confirmation.
"""
import json
import os
import sys
from pathlib import Path
from dataclasses import replace
import numpy as np
from scipy.optimize import minimize
from scipy.special import expit
from scipy.stats import rankdata
sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluate_tail_pairwise as tail
import evaluate_next_five_combinations as combo


def top_pair_weights(current, left, right):
    """Fixed top-emphasis based only on earlier training scores, never target labels."""
    rank = rankdata(-np.asarray(current), method='average')
    # Smooth reciprocal-rank discount avoids an arbitrary training cutoff.
    discount = 1 / np.log2(rank + 1)
    return np.maximum(discount[left], discount[right])


def fit_top(features, ratings, current, c):
    model = tail.fit_pairwise_ranker(features, ratings, current, 0)
    x = tail.transform_features(features, model.feature_scaler)
    y = np.asarray(ratings)
    base = (current - model.current_mean) / model.current_scale
    left, right = np.triu_indices(len(y), 1)
    keep = y[left] != y[right]
    left, right = left[keep], right[keep]
    direction = np.sign(y[left] - y[right])
    dx, db = x[left] - x[right], base[left] - base[right]
    w = top_pair_weights(current, left, right)
    w /= w.sum()
    def objective(beta):
        margin = direction * (db + dx @ beta)
        loss = np.dot(w, np.logaddexp(0, -margin)) + np.dot(beta, beta)/(2*c)
        gradient = dx.T @ (-w*direction*expit(-margin)) + beta/c
        return float(loss), gradient
    result = minimize(objective, np.zeros(x.shape[1]), jac=True, method='L-BFGS-B',
                      options={'maxiter':2000, 'ftol':1e-12, 'gtol':1e-8})
    if not result.success or not np.isfinite(result.x).all():
        raise ValueError('Top-weighted fit did not converge')
    return replace(model, coefficients=result.x, c=c, objective=float(result.fun),
                   optimizer_iterations=int(result.nit))


def pool_metrics(y, scores, ids):
    if not len(y) or not np.isfinite(scores).all():
        raise ValueError('Expected finite nonempty common pool')
    # Tie-breaking independent of ratings, identical across all arms.
    order = np.lexsort((ids, -scores))
    result = combo.metrics(y, scores)
    for k in (8,20):
        top = order[:min(k,len(y))]
        result[f'top{k}_high_precision'] = float((y[top]>=4).mean())
        result[f'top{k}_dislike_inclusion'] = float((y[top]<=2).mean())
        gain = 2**(y-1)-1
        discount = 1/np.log2(np.arange(len(top))+2)
        ideal = np.sort(gain)[::-1][:len(top)] @ discount
        result[f'ndcg{k}'] = float(gain[top]@discount/ideal) if ideal else 0.0
    return result


def run(feature_path, manifest_path, output):
    feature_path, manifest_path, output = map(Path,(feature_path,manifest_path,output))
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output,0o700)
    manifest = json.loads(manifest_path.read_text())
    if tail.file_sha256(feature_path) != manifest['feature_artifact_sha256']:
        raise ValueError('Feature manifest mismatch')
    artifact = tail.load_feature_artifact(feature_path)
    folds = tail.development_folds_from_manifest(artifact,manifest)
    n = len(artifact.ratings)
    # Registration is written before fitting or looking at outcome metrics.
    protocol = {'created_at':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
                'primary':'mean fold top8 favorite precision', 'guardrails':['top8 dislike inclusion','balanced high/low AUC'],
                'tie_break':'read_id ascending', 'c_grid':[.1,1.,10.],
                'arms':['current','old_pairwise','top_weighted_pairwise','cluster3'],
                'pool':'each whole-day chronological evaluation block of eventually-read books',
                'labels':'previously inspected; development/regression evidence only',
                'feature_sha256':tail.file_sha256(feature_path),
                'correction_cap':'25% earlier current-score SD, max5 points',
                'weight':'max reciprocal log2 earlier base-score rank of pair',
                'selection':'top8 high precision; ties lower dislikes, greater balanced AUC, smaller C'}
    protocol_path=output/'protocol-private.json'
    if protocol_path.exists():
        registered=json.loads(protocol_path.read_text())
        if {k:v for k,v in registered.items() if k!='created_at'} != {k:v for k,v in protocol.items() if k!='created_at'}:
            raise ValueError('Existing protocol differs; use a separate output directory')
        protocol=registered
    else:
        write(protocol_path,protocol)
    fold_specs = folds + [(np.arange(int(artifact.train_mask.sum())),np.flatnonzero(artifact.validation_mask)),
                         (np.arange(int((artifact.train_mask|artifact.validation_mask).sum())),np.flatnonzero(artifact.later_mask))]
    all_scores = {'current':artifact.current.copy()}
    reports = {}
    selected = {}
    for family, fit in [('old_pairwise',tail.fit_pairwise_ranker),('top_weighted_pairwise',fit_top)]:
        candidates = {}
        preds = {}
        for c in (.1,1.,10.):
            scores = np.full(n,np.nan)
            fold_rows = []
            for train,evaluate in folds:
                model = fit(artifact.base_features[train],artifact.ratings[train],artifact.current[train],c)
                scores[evaluate] = tail.score_pairwise_ranker(model,artifact.base_features[evaluate],artifact.current[evaluate])
                fold_rows.append(pool_metrics(artifact.ratings[evaluate],scores[evaluate],artifact.read_ids[evaluate]))
            candidates[c] = fold_rows
            preds[c] = scores
        def key(c):
            rows=candidates[c]
            return (np.mean([r['top8_high_precision'] for r in rows]),
                    -np.mean([r['top8_dislike_inclusion'] for r in rows]),
                    np.mean([r['balanced_auc'] for r in rows]),-c)
        chosen = max(candidates,key=key)
        selected[family]=chosen
        scores=preds[chosen]
        for train,evaluate in fold_specs[3:]:
            model=fit(artifact.base_features[train],artifact.ratings[train],artifact.current[train],chosen)
            scores[evaluate]=tail.score_pairwise_ranker(model,artifact.base_features[evaluate],artifact.current[evaluate])
        all_scores[family]=scores
        reports[family]={'selected_c':chosen,'development_grid':candidates}
    cluster=np.full(n,np.nan)
    for train,evaluate in fold_specs:
        signal=artifact.signals['cluster'] if 'cluster' in artifact.signals else artifact.signals['cluster3']
        scale=artifact.current[train].std()
        sd=signal[train].std()
        delta= .25*scale*(signal[evaluate]-signal[train].mean())/max(sd,1e-8)
        cluster[evaluate]=combo.served(artifact.current[evaluate]+np.clip(delta,-min(5,.25*scale),min(5,.25*scale)))
    all_scores['cluster3']=cluster
    sections={}
    for idx,(_,evaluate) in enumerate(fold_specs):
        label=f'development_fold_{idx+1}' if idx<3 else ('previous_validation' if idx==3 else 'previous_later')
        sections[label]={name:pool_metrics(artifact.ratings[evaluate],scores[evaluate],artifact.read_ids[evaluate]) for name,scores in all_scores.items()}
        for name,scores in all_scores.items():
            sections[label][name]['top8_baseline_overlap']=len(set(artifact.read_ids[evaluate][np.lexsort((artifact.read_ids[evaluate],-scores[evaluate]))[:8]])&set(artifact.read_ids[evaluate][np.lexsort((artifact.read_ids[evaluate],-artifact.current[evaluate]))[:8]]))
    for idx, (_, evaluate) in enumerate(fold_specs):
        label = f'development_fold_{idx+1}' if idx<3 else ('previous_validation' if idx==3 else 'previous_later')
        base_order=np.lexsort((artifact.read_ids[evaluate],-artifact.current[evaluate]))
        base_rank=np.empty(len(evaluate),dtype=int);base_rank[base_order]=np.arange(len(evaluate))
        for name,scores in all_scores.items():
            order=np.lexsort((artifact.read_ids[evaluate],-scores[evaluate]))
            rank=np.empty(len(evaluate),dtype=int);rank[order]=np.arange(len(evaluate))
            displacement=np.abs(rank-base_rank)
            sections[label][name]['rank_stability']={'changed_positions':int((displacement>0).sum()),
                'max_displacement':int(displacement.max()),'mean_displacement':float(displacement.mean())}
    scores_path=output/'scores-private.npz'
    np.savez_compressed(scores_path,read_ids=artifact.read_ids,**{key:getattr(artifact,key) for key in ('train_mask','validation_mask','later_mask','oof_selection_mask')},**{'score__'+k:v for k,v in all_scores.items()})
    os.chmod(scores_path,0o600)
    report={'protocol':protocol,'selected':selected,'models':reports,'results':sections,
            'limitations':['Previously inspected outcomes, no fresh confirmation','Synthetic blocks are not real eligible slates','Pair terms are not independent observations','No quantitative guardrail tolerance was preregistered; no arm certified for adoption'], 'adoption':'No first-eight favorite precision improvement; do not promote'}
    write(output/'aggregate.json',report)
    print(json.dumps({'selected':selected,'results':sections},indent=2))
    return report


def write(path,value):
    path.write_text(json.dumps(value,indent=2,sort_keys=True)+'\n')
    os.chmod(path,0o600)

if __name__=='__main__':
    run(*sys.argv[1:])
