# Tail-head and pairwise ranking tests: October 2, 2026

Neither the separate tail heads nor the pairwise residual shows a reliable
standalone OOF gain over the current ranker. The primary-view pairwise result is
effectively tied with current; the heads lose materially in both feature views.
Validation gains for the heads are recorded below, but they do not reverse the
OOF evidence or establish a standalone selection win. The root aggregate owns
the separate comparison of cross-family combinations.

The frozen export contains 1,677 eligible reads after the first 100 reads
establish history: 966 development, 355 validation, and 356 later exploratory
targets. Each query hides all reads from its UTC day. The existing ESS5 scorer
reproduced all 1,677 served scores exactly. Development configuration selection
uses only three forward, whole-day OOF folds (200, 200, and 266 evaluation
reads); the first 300 development rows are fit-only and have no OOF predictions.

| Feature view | Current OOF balanced AUC | Tail heads | Pairwise residual | Pairwise change vs current |
| --- | ---: | ---: | ---: | ---: |
| Primary | 0.60119 | 0.55189 | 0.60124 | +0.00006 |
| Rich | 0.59221 | 0.54693 | 0.59151 | −0.00070 |

The selected regularization was `C=0.1` for both families in both views, from
the frozen grid `{0.1, 1, 10}`. The heads' primary OOF mean was 0.55641 high AUC
and 0.54738 low-tail reversed AUC, compared with 0.59677 and 0.60561 for
current. In the rich view, the heads averaged 0.55852 high and 0.53533 low,
compared with 0.59892 and 0.58550 for rich current. Pairwise changes were tiny:
primary high −0.00030 and low +0.00041; rich high −0.00072 and low −0.00068.

The validation point estimates are more favorable for the heads: primary heads
are +0.00759 balanced AUC versus primary current, and rich heads are +0.04613
versus rich current. Rich heads are +0.01397 versus the matched-query control,
which uses the same rich query with the original title-author history. The
pairwise arm is +0.00182 versus primary current and +0.00295 versus rich
current. These are confirmation-period summaries, not a basis for replacing
the OOF selection result; the rich comparisons also keep their three reference
scorers distinct.

The high head predicts `rating >= 4`; the low head predicts `rating <= 2`.
Both logistic models use every training rating, so three-star reads are
negative for both heads and neutral between the two endpoints. Their utility is
`50 + 50 * (p_high - p_low)`, clipped to 0–100 and rounded with Python's
one-decimal serving rule. The probabilities are model outputs, not calibrated
enjoyment likelihoods.

The pairwise model fits a regularized residual around the standardized current
score using every unequal-rating unordered training pair. Its objective is
mean logistic loss plus `0.5 * ||beta||^2 / C`; pair terms are not treated as
independent observations. Features and the current-score offset are fitted from
the training prefix only. At serving time the residual is capped at
`±min(5 points, 0.25 * training current-score SD)`, clipped to 0–100, and
rounded to a tenth.

The exports also include the frozen heads/pairwise 50:50 blend, 25:75 blends
with current, and fixed 0.25-training-SD signal blends with recency, cluster3,
ordinal expected rating, and the recency-plus-cluster3 pair. Signal scalers use
only the applicable training prefix. For the first fold's ordinal scaler only,
an ordinal fit on that fold's training prefix supplies in-sample prefix values;
the missing OOF rows remain missing and are never used as evaluation scores.
The primary pairwise arm's nominal `+0.00006` is the only in-family OOF
exceedance over current; its high-tail point estimate falls while its low-tail
point estimate rises, and the gain is effectively zero at this sample size.
Every other fixed in-family blend is below current in both views. This does
not select or reject combinations with the other experiment families.

The later period had been inspected in earlier work and remains descriptive.
The study is retrospective for one reader and does not establish live
recommendation lift. No individual reading identities or rating rows are
included here. Identity-aligned score NPZs remain in the private study folder
with mode `0600`.

## Reproducibility hashes

| Artifact | SHA-256 |
| --- | --- |
| Frozen protocol | `8c58739a896acb2d0284f3c51eb5752121ed9262a415ecd313b8f124d7dd935d` |
| Primary feature NPZ | `43e2c09b6251b9bb6fee521cb5df565952f2ee9306c3afb3850d51518a88c329` |
| Rich feature NPZ | `ac30a796e11ecf7ea01af009645bdd3c8ee2e8cf894e1c1fbe0be22e5e4d0b0e` |
| Tail/pairwise evaluator | `18fffc667006898b981d72bfe1682294bc3a97564fad0163b50cdba5a2e44e8b` |
| Primary score NPZ | `f39ad82420fb023f3cd66b41dbfe6997854f3e221edfc70c8f2bb03757d62ab6` |
| Rich score NPZ | `695c614ea252b1760b51c17f36700e9374d42be0f0ffb234dd5fb0f98619817a` |
| Aggregate JSON | `7c668ed3c7c81f902bfef7e795e1b43672499f97c1a80916e92268f861fb1ca8` |
