import sys
from types import SimpleNamespace
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
import evaluate_first_eight as ev


def test_top_weights_emphasize_earlier_base_rank_and_ties_equally():
    scores=np.array([90.,50.,50.,10.])
    weights=ev.top_pair_weights(scores,np.array([0,1,2]),np.array([3,3,3]))
    assert weights[0]>weights[1]
    assert weights[1]==weights[2]


def test_top_pool_metrics_ties_use_ids_not_rating_order():
    y=np.array([5,1,4,2,5,1,4,2,5.])
    ids=np.arange(9)[::-1]
    scores=np.ones(9)*50
    result=ev.pool_metrics(y,scores,ids)
    assert result['top8_high_precision']==4/8
    assert result['top8_dislike_inclusion']==4/8


def test_top_fit_has_training_scaler_and_fixed_cap():
    x=np.array([[0.,1.],[1.,0.],[2.,1.],[3.,0.],[4.,1.]])
    y=np.array([1,2,3,4,5])
    current=np.array([50.,51.,52.,53.,54.])
    model=ev.fit_top(x,y,current,.1)
    np.testing.assert_allclose(model.feature_scaler.mean,x.mean(axis=0))
    held=np.array([[1000.,-1000.]])
    prediction=ev.tail.score_pairwise_ranker(model,held,np.array([52.]))
    assert abs(prediction[0]-52)<=model.correction_cap_points+.05


def test_top_fit_matches_reference_weighted_loss_and_gradient(monkeypatch):
    x=np.array([[0.,1.],[1.,0.],[2.,1.],[3.,0.],[4.,1.]])
    y=np.array([1,2,3,4,5])
    current=np.array([50.,51.,52.,53.,54.])
    c=.7
    beta=np.array([.2,-.3])

    def check_objective(objective, initial, *, jac, method, options):
        assert jac is True
        assert method=='L-BFGS-B'
        value, gradient=objective(beta)
        model=ev.tail.fit_pairwise_ranker(x,y,current,0)
        z=ev.tail.transform_features(x,model.feature_scaler)
        base=(current-model.current_mean)/model.current_scale
        left,right=np.triu_indices(len(y),1)
        keep=y[left]!=y[right]
        left,right=left[keep],right[keep]
        direction=np.sign(y[left]-y[right])
        delta_features=z[left]-z[right]
        delta_current=base[left]-base[right]
        weights=ev.top_pair_weights(current,left,right)
        weights=weights/weights.sum()
        margins=direction*(delta_current+delta_features@beta)
        expected_value=(np.dot(weights,np.logaddexp(0.,-margins))
                        +np.dot(beta,beta)/(2*c))
        expected_gradient=(delta_features.T@(-weights*direction*ev.expit(-margins))
                           +beta/c)
        np.testing.assert_allclose(value,expected_value,rtol=1e-12,atol=1e-12)
        np.testing.assert_allclose(gradient,expected_gradient,rtol=1e-12,atol=1e-12)
        return SimpleNamespace(x=beta,success=True,fun=value,nit=1)

    monkeypatch.setattr(ev,'minimize',check_objective)
    fitted=ev.fit_top(x,y,current,c)
    np.testing.assert_array_equal(fitted.coefficients,beta)
