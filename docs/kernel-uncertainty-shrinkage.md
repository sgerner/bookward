# Kernel uncertainty shrinkage

The serving ranker reduces the rating-neighborhood adjustment when its similarity
weight is concentrated in only a few reads. The selected formula is the ESS5 arm
from the [October 1 synergy study](ranking-synergy-2026-10-01.md).

For the existing 40-neighbor, cosine-to-the-eighth-power weights, define effective
sample size as `ESS = sum(weights)^2 / sum(weights^2)`. Multiply the existing
kernel rating adjustment by `ESS / (ESS + 5)`. One dominant neighbor retains
about one-sixth of the adjustment; 40 equally weighted neighbors retain eight-ninths.
This reduces both upward and downward adjustments. The history-size ramp still
applies, and same-work exclusion and deduplication precede the calculation.

## Decision and evidence

We recommend this as a small reliability correction. Average high/low AUC
improved in validation and the later period, and the change requires no new
metadata or embeddings. The earlier study chose to preserve its baseline;
this follow-up accepts the modest average gain and its documented tradeoff.
The evidence does not establish a live recommendation benefit.

| Period | Previous high / low AUC | ESS5 high / low AUC | Previous / ESS5 average AUC | Previous / ESS5 top-high, bottom-low counts |
| --- | --- | --- | --- | --- |
| Validation, 355 books | .5240 / .5917 | .5244 / .5939 | .5579 / .5591 | 8, 4 / 7, 5 |
| Later, 356 books | .6021 / .5429 | .5988 / .5500 | .5725 / .5744 | 14, 5 / 14, 5 |

ESS5 was selected from the fixed eight-arm author/uncertainty/negative-neighbor
factorial using validation average AUC. The later high-AUC change is −.0033;
low AUC improves .0071. Paired whole-calendar-day 95% intervals include zero:
high [−.0079, .0017], low [−.0021, .0198]. The later period was reused in prior
studies; these are exploratory results from one reader's chosen books.

## Enrichment blend

The exact production read-symmetric 75/25 score blend remains promising, with
validation average AUC +.0021 and later average AUC +.0045 on all targets.
Its second later half loses .0049 average AUC and .0199 low AUC, and its
metadata-covered later bottom-20 low precision falls from .20 to .15.
The blend was examined after constituent outcomes were known and was not the
validation-selected fusion arm.

A matched-score follow-up with ESS5 included does not resolve that concern.
The symmetric blend improves average AUC .0015 on the 191 metadata-covered
validation targets and .0054 across all 356 later targets, but loses .0025
average AUC and .0164 low AUC in the second later half. The closer production
candidate-query text format instead loses .0007 average AUC on validation.
This is another descriptive comparison on the existing arrays, without a
weight search or new independent outcomes.

Serving that blend would also require verified read metadata, a second embedding
view, and the symmetric target representation tested in the experiment.
Current candidate documents use a different text layout. We therefore recommend
retaining the blend for a new evaluation rather than adopting it as the default.

The study's private corpus, embeddings, and reading records remain outside the
repository. Historical study reports retain their original baseline labels;
new evaluator runs use the deployed formula as `current` and retain the previous
unshrunk kernel as a comparator.

The serving implementation and optimized evaluator reproduce all 1,677 archived
ESS5 target scores exactly at the displayed one-decimal precision. Reproducing
an older study's complete grid requires that study's code revision; its archived
`current` label refers to the formula deployed when the study was run.
