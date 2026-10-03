import json
from pathlib import Path
import importlib.util
import sys
from types import SimpleNamespace
import numpy as np
import pytest

scripts=Path(__file__).parents[1]/'scripts'
sys.path.insert(0,str(scripts))
spec=importlib.util.spec_from_file_location('track_combinations',scripts/'evaluate_track_combinations.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_combined_residual_has_shared_cap_and_no_disabled_drift():
    base=np.array([50.,90.,2.])
    embedding=np.array([54.,99.,0.])
    top=np.array([54.,99.,0.])
    np.testing.assert_equal(module.compose(base,embedding,top,5.,True,True),[55.,95.,0.])
    np.testing.assert_equal(module.compose(base,embedding,top,5.,False,False),base)
    with pytest.raises(ValueError,match='Missing causal'):
        module.compose(base,np.array([np.nan,99.,0.]),top,5.,True,False)
    # Three individually valid corrections still share one total budget.
    np.testing.assert_equal(module.compose(base,embedding,top,5.,True,True,
                                          metric=embedding,use_metric=True),[55.,95.,0.])
    with pytest.raises(ValueError,match='Missing metric'):
        module.compose(base,embedding,top,5.,False,False,use_metric=True)


def test_alignment_refuses_shuffled_identities_and_split_drift(tmp_path):
    f={'read_ids':np.array([1,2]),'current':np.array([50.,60.]),'train_mask':np.array([True,False]),'validation_mask':np.array([False,True]),'later_mask':np.array([False,False]),'oof_selection_mask':np.array([False,True])}
    path=tmp_path/'scores.npz'
    np.savez(path,read_ids=np.array([2,1]),score=np.array([60.,50.]))
    with pytest.raises(ValueError,match='identities'):
        module.aligned(path,f,['score'])
    np.savez(path,read_ids=f['read_ids'],train_mask=np.array([False,True]),score=np.array([50.,60.]))
    with pytest.raises(ValueError,match='split'):
        module.aligned(path,f,['score'])


def test_run_rejects_unfrozen_embedding_key_before_loading_score_artifacts(tmp_path):
    features=tmp_path/'features.npz'
    np.savez(
        features,
        read_ids=np.arange(4),
        current=np.asarray([50.,51.,52.,53.]),
        utc_day=np.arange(4),
        train_mask=np.asarray([True,True,True,False]),
        validation_mask=np.asarray([False,False,False,False]),
        later_mask=np.asarray([False,False,False,True]),
        oof_selection_mask=np.asarray([False,True,True,False]),
    )
    manifest=tmp_path/'manifest.json'
    manifest.write_text(json.dumps({
        'feature_artifact_sha256':module.combo.sha(features),
        'development_folds':[{'train_stop':1,'evaluation_start':1,'evaluation_stop':3}],
    }))
    protocol=tmp_path/'protocol.json'
    protocol.write_text(json.dumps({
        'appeal':'source gate failed; disabled equals current, yielding four distinct configurations',
    }))
    args=SimpleNamespace(
        features=features,manifest=manifest,protocol=protocol,
        embedding=tmp_path/'missing-embedding.npz',
        first_eight=tmp_path/'missing-first-eight.npz',
        appeal=tmp_path/'missing-appeal.npz',
        embedding_key='some_other_embedding_prediction',
        metric=None,extended_protocol=None,
    )

    with pytest.raises(ValueError,match='frozen prediction key'):
        module.run(args)


def test_extended_factor_contract_names_selected_prediction_inputs():
    extension={
        'distinct_configurations':8,
        'factors':list(module.EXTENDED_FACTORS),
        'metric_factor':module.EXTENDED_METRIC_FACTOR,
        'metric_source_protocol_sha256':'b'*64,
        'source_binding_amendment_sha256':'c'*64,
    }
    module.validate_factor_contract(module.SELECTED_EMBEDDING_KEY,extension)

    extension['factors'][0]='unselected_embedding_score'
    with pytest.raises(ValueError,match='factors'):
        module.validate_factor_contract(module.SELECTED_EMBEDDING_KEY,extension)

    extension['factors']=list(module.EXTENDED_FACTORS)
    extension['metric_factor']='use a different metric blend'
    with pytest.raises(ValueError,match='aligned_metric 75/25'):
        module.validate_factor_contract(module.SELECTED_EMBEDDING_KEY,extension)


def test_metric_predictions_must_match_frozen_features_and_source_protocol(tmp_path):
    path=tmp_path/'metric.npz'
    feature_hash='a'*64
    protocol_hash='b'*64
    np.savez(
        path,
        features_sha256=np.asarray(feature_hash),
        protocol_sha256=np.asarray(protocol_hash),
        aligned_metric=np.asarray([50.,51.]),
    )

    assert module.validate_metric_provenance(path,feature_hash,protocol_hash)==protocol_hash
    with pytest.raises(ValueError,match='different feature artifact'):
        module.validate_metric_provenance(path,'c'*64,protocol_hash)
    with pytest.raises(ValueError,match='pinned source protocol'):
        module.validate_metric_provenance(path,feature_hash,'d'*64)
