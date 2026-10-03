#!/usr/bin/env python3
"""Bounded factorial of causal representation and first-eight corrections.

Appeal is disabled when its preregistered source gate fails. This screen reuses
previously inspected ratings; it cannot establish a confirmatory policy win.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import numpy as np
import evaluate_next_five_combinations as combo
import evaluate_first_eight as first

SELECTED_EMBEDDING_KEY = "selected_capped_residual_score"
EXTENDED_FACTORS = (
    "strictv2_development_selected_capped_embedding_residual",
    "unchanged_development_selected_topweighted_pairwise",
    "corrected_development_selected_metric_current75_aligned25_residual",
)
EXTENDED_METRIC_FACTOR = (
    "Use prior frozen selected metric fit/calibration and 75/25 blending rule; "
    "difference from current is contribution. No rich-arm or weight reselection "
    "from later labels."
)


def validate_factor_contract(embedding_key, extension=None):
    """Refuse prediction-array substitutions inconsistent with the frozen arms."""
    if embedding_key != SELECTED_EMBEDDING_KEY:
        raise ValueError(
            f"Embedding factor requires the frozen prediction key {SELECTED_EMBEDDING_KEY!r}"
        )
    if extension is None:
        return
    if extension.get("distinct_configurations") != 8:
        raise ValueError("Expected the frozen eight-arm synergy contract")
    if tuple(extension.get("factors", ())) != EXTENDED_FACTORS:
        raise ValueError("Extended protocol factors do not match the implemented predictions")
    if extension.get("metric_factor") != EXTENDED_METRIC_FACTOR:
        raise ValueError("Extended protocol metric factor does not match aligned_metric 75/25")
    source_protocol_hash = extension.get("metric_source_protocol_sha256")
    if not isinstance(source_protocol_hash, str) or not re.fullmatch(
        r"[0-9a-f]{64}", source_protocol_hash
    ):
        raise ValueError("Extended protocol must pin the source metric protocol SHA-256")
    amendment_hash = extension.get("source_binding_amendment_sha256")
    if not isinstance(amendment_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", amendment_hash):
        raise ValueError("Extended protocol must reference its metadata-only source-binding amendment")


def validate_metric_provenance(path, features_sha256, source_protocol_sha256):
    """Bind the selected aligned metric predictions to their frozen source inputs."""
    with np.load(path, allow_pickle=False) as archive:
        observed_features_hash = (
            archive["features_sha256"] if "features_sha256" in archive.files else None
        )
        observed_protocol_hash = (
            archive["protocol_sha256"] if "protocol_sha256" in archive.files else None
        )
        if (
            observed_features_hash is None
            or observed_features_hash.shape != ()
            or observed_features_hash.dtype.kind not in {"U", "S"}
        ):
            raise ValueError("Metric archive lacks its frozen feature-artifact hash")
        if (
            observed_protocol_hash is None
            or observed_protocol_hash.shape != ()
            or observed_protocol_hash.dtype.kind not in {"U", "S"}
        ):
            raise ValueError("Metric archive lacks its source protocol hash")
        observed_features_hash = str(observed_features_hash.item())
        observed_protocol_hash = str(observed_protocol_hash.item())
    if observed_features_hash != features_sha256:
        raise ValueError("Metric predictions came from a different feature artifact")
    if observed_protocol_hash != source_protocol_sha256:
        raise ValueError("Metric predictions do not match the pinned source protocol")
    return observed_protocol_hash


def aligned(path, features, keys):
    with np.load(path, allow_pickle=False) as z:
        if not np.array_equal(z['read_ids'], features['read_ids']):
            raise ValueError('Different target identities or order')
        for mask in ('train_mask', 'validation_mask', 'later_mask', 'oof_selection_mask'):
            if mask not in z or not np.array_equal(z[mask], features[mask]):
                raise ValueError('Different chronological split')
        values = {key:z[key].copy() for key in keys}
    for value in values.values():
        if value.shape != features['current'].shape or np.isinf(value).any():
            raise ValueError('Malformed aligned predictions')
    return values


def compose(current, embedding, top, cap, use_embedding, use_top, metric=None, use_metric=False):
    correction = np.zeros_like(current, dtype=float)
    if use_embedding:
        correction += embedding - current
    if use_top:
        correction += top - current
    if use_metric:
        if metric is None:
            raise ValueError('Missing metric contribution')
        correction += metric - current
    if not np.isfinite(correction).all():
        raise ValueError('Missing causal predictions in evaluation slice')
    return combo.served(current + np.clip(correction, -cap, cap))


def run(args):
    with np.load(args.features, allow_pickle=False) as z:
        f = {key:z[key].copy() for key in z.files}
    manifest = json.loads(args.manifest.read_text())
    features_sha256 = combo.sha(args.features)
    if features_sha256 != manifest['feature_artifact_sha256']:
        raise ValueError('Frozen feature checksum mismatch')
    combo.validate_folds(f, manifest)
    protocol = json.loads(args.protocol.read_text())
    if protocol['appeal'] != 'source gate failed; disabled equals current, yielding four distinct configurations':
        raise ValueError('This bounded evaluator requires disabled appeal')
    metric_path = getattr(args, 'metric', None)
    extension_path = getattr(args, 'extended_protocol', None)
    if bool(metric_path) != bool(extension_path):
        raise ValueError('Metric synergy requires a frozen extension protocol')
    extension = json.loads(extension_path.read_text()) if extension_path else None
    validate_factor_contract(args.embedding_key, extension)
    metric_source_protocol_sha256 = None
    if metric_path:
        metric_source_protocol_sha256 = validate_metric_provenance(
            metric_path,
            features_sha256,
            extension['metric_source_protocol_sha256'],
        )

    embedding = aligned(args.embedding, f, [args.embedding_key])[args.embedding_key]
    top_values = aligned(args.first_eight, f, ['score__top_weighted_pairwise','score__old_pairwise','score__cluster3'])
    top = top_values['score__top_weighted_pairwise']
    metric = None
    if metric_path:
        raw_metric = aligned(metric_path, f, ['aligned_metric'])['aligned_metric']
        metric = combo.served(.75*f['current'] + .25*raw_metric)
    appeal = aligned(args.appeal, f, ['score__experience_disabled_fallback'])['score__experience_disabled_fallback']
    if not np.array_equal(appeal, f['current']):
        raise ValueError('Disabled appeal unexpectedly changes scores')
    n = len(f['current'])
    specs = [(row['train_stop'], np.arange(row['evaluation_start'], row['evaluation_stop']))
             for row in manifest['development_folds']]
    specs += [(int(f['train_mask'].sum()),np.flatnonzero(f['validation_mask'])),
              (int((f['train_mask']|f['validation_mask']).sum()),np.flatnonzero(f['later_mask']))]
    arms = {'current':f['current'].copy()}
    configurations = [('embedding',True,False,False),('top_weighted',False,True,False),('embedding_plus_top',True,True,False)]
    if metric is not None:
        configurations += [('metric',False,False,True),('embedding_plus_metric',True,False,True),
                           ('top_weighted_plus_metric',False,True,True),('embedding_plus_top_plus_metric',True,True,True)]
    for name, e, t, m in configurations:
        values = np.full(n,np.nan)
        for stop, idx in specs:
            cap = min(5., .25*float(f['current'][:stop].std()))
            values[idx] = compose(f['current'][idx],embedding[idx],top[idx],cap,e,t,
                                  None if metric is None else metric[idx],m)
        arms[name] = values
    arms.update({key.removeprefix('score__'):value for key,value in top_values.items() if key!='score__top_weighted_pairwise'})
    factorial = ('current', *(row[0] for row in configurations))
    sections = {}
    for number, (_, idx) in enumerate(specs):
        name = f'development_fold_{number+1}' if number<3 else ('previous_validation' if number==3 else 'previous_later')
        sections[name] = {arm:first.pool_metrics(f['ratings'][idx],scores[idx],f['read_ids'][idx]) for arm,scores in arms.items()}
    means = {arm:float(np.mean([sections[f'development_fold_{i}'][arm]['balanced_auc'] for i in (1,2,3)])) for arm in factorial}
    complexity = {'current':0, **{name:sum((e,t,m)) for name,e,t,m in configurations}}
    chosen = sorted(factorial,key=lambda arm:(-means[arm],complexity[arm],arm))[0]
    prediction_keys = {
        'embedding': args.embedding_key,
        'top_weighted': 'score__top_weighted_pairwise',
        'metric': 'aligned_metric' if metric is not None else None,
    }
    selection = {'chosen':chosen,'development_mean_balanced_auc':means,'prediction_keys':prediction_keys,'selection_inputs':{name:combo.sha(path) for name,path in [('features',args.features),('manifest',args.manifest),('protocol',args.protocol),('embedding',args.embedding),('first_eight',args.first_eight),('appeal',args.appeal)]}}
    if metric_path:
        selection['selection_inputs'].update(metric=combo.sha(metric_path),extended_protocol=combo.sha(extension_path))
        selection['metric_source_protocol_sha256'] = metric_source_protocol_sha256
    # Selection is fixed solely by the development folds; later results are descriptive.
    uncertainty = {}
    for name, mask in [('previous_validation',f['validation_mask']),('previous_later',f['later_mask'])]:
        uncertainty[name] = {group:combo.grouped_bootstrap(f['ratings'][mask],f['current'][mask],arms[chosen][mask],values[mask],args.repetitions)
                             for group,values in [('UTC_day',f['utc_day']),('author',f['author_group']),('30_day_block',f['utc_day']//30)]}
    result = {'protocol':protocol,'extension_protocol':extension,'prediction_keys':prediction_keys,'metric_source_protocol_sha256':metric_source_protocol_sha256,'selection':selection,'nominal_configurations':2*len(factorial),'distinct_configurations':len(factorial),
              'disabled_appeal_duplicate_factor':2,'references_excluded_from_selection':['old_pairwise','cluster3'],
              'results':sections,'selected_uncertainty':uncertainty,
              'limitations':['Historical outcomes repeatedly inspected; development evidence only','Eventually-read synthetic pools do not represent real discovery slates','No quantitative adoption guardrail was preregistered','Intervals are conditional on the selected arm and do not adjust for model selection','Pooled cross-fold score comparisons are not the within-fold selection endpoint']}
    args.output.mkdir(parents=True,exist_ok=True,mode=0o700)
    combo.private_json(args.output/'aggregate.json',result)
    combo.private_json(args.output/'selection-private.json',selection)
    path = args.output/'scores-private.npz'
    np.savez_compressed(path,read_ids=f['read_ids'],**{key:f[key] for key in ('train_mask','validation_mask','later_mask','oof_selection_mask')},**{'score__'+key:value for key,value in arms.items()})
    path.chmod(0o600)
    print(json.dumps({'selection':selection,'results':sections,'uncertainty':uncertainty},indent=2))
    return result

if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('features','manifest','protocol','embedding','first-eight','appeal','output'):
        p.add_argument('--'+key,type=Path,required=True)
    p.add_argument('--embedding-key',required=True)
    p.add_argument('--metric',type=Path)
    p.add_argument('--extended-protocol',type=Path)
    p.add_argument('--repetitions',type=int,default=2000)
    run(p.parse_args())
