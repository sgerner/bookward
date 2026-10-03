#!/usr/bin/env python3
"""Label-blind source pilot and fixed causal screen for reading-experience cues.

The pilot uses only identity-aligned, source-verified synopsis text and writes
all item-level evidence outside the repository. The optional rating screen is
retrospective development evidence for one reader, not a live utility result.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys

import numpy as np
from sklearn.linear_model import Ridge

ENGINE = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(SCRIPTS))

import evaluate_metric_facets as grounded
import evaluate_next_five_combinations as metrics
from afterword_engine.ingestion import _read_metadata_identity_hash
from afterword_engine.subjects import normalize_subjects


PROTOCOL_VERSION = 3
PREVIOUS_SCRIPT_SHA256 = "e5aa8ca935590b53c1e3f4beae1236ca6bea7ce41b88713f7c509242c85efadf"
RATING_SCALE = 25.0
RIDGE_ALPHA = 10.0
TARGET_SAMPLE_SIZE = 60
SAMPLE_SEED = "bookward-experience-pilot-20261002-v1"
MAX_CORRECTION_POINTS = 5.0
CORRECTION_SD_FRACTION = 0.25

# These exact phrases are the complete extraction schema. The extractor never
# sees a title, rating, review, or model-generated summary.
EXPERIENCE_CUES: dict[str, tuple[str, ...]] = {
    "pace_fast": (
        "fast-paced", "fast paced", "fast-moving", "fast moving",
        "page-turner", "page turner", "propulsive", "briskly paced",
        "rapid-fire",
    ),
    "pace_slow": (
        "slow-burn", "slow burn", "slow-paced", "slow paced",
        "deliberate pace", "unhurried pace", "measured pace",
    ),
    "tone_dark": (
        "dark in tone", "bleak", "grim", "haunting", "unsettling",
        "chilling",
    ),
    "tone_humorous": (
        "witty", "humorous", "laugh-out-loud", "laugh out loud",
        "satirical", "lighthearted", "light-hearted",
    ),
    "tone_hopeful": (
        "hopeful", "uplifting", "heartwarming", "heart-warming",
        "optimistic", "inspiring",
    ),
    "tone_tense": (
        "suspenseful", "tense", "taut", "edge-of-your-seat",
        "edge of your seat",
    ),
    "density_technical": (
        "highly technical", "technical detail", "technical prose",
        "dense prose", "densely written", "intricate prose",
        "complex prose",
    ),
    "density_spare_accessible": (
        "spare prose", "sparse prose", "minimalist prose",
        "accessible prose", "straightforward prose",
    ),
    "structure_multiple_perspectives": (
        "multiple perspectives", "alternating perspectives",
        "dual perspectives", "dual points of view",
        "told from multiple perspectives", "interwoven perspectives",
    ),
    "structure_nonlinear": (
        "nonlinear narrative", "non-linear narrative",
        "nonlinear structure", "non-linear structure", "fractured timeline",
        "alternating timelines",
    ),
    "structure_episodic": (
        "episodic narrative", "episodic structure", "linked stories",
        "interconnected stories", "story cycle", "vignettes",
    ),
}
EXPERIENCE_GROUPS = {
    "pace": tuple(name for name in EXPERIENCE_CUES if name.startswith("pace_")),
    "tone": tuple(name for name in EXPERIENCE_CUES if name.startswith("tone_")),
    "density": tuple(name for name in EXPERIENCE_CUES if name.startswith("density_")),
    "structure": tuple(name for name in EXPERIENCE_CUES if name.startswith("structure_")),
}

PILOT_COVERAGE_GATE = {
    "minimum_any_supported_cue": 100,
    "minimum_supported_per_group": 20,
    "minimum_supported_groups": 3,
    "minimum_per_group_in_initial_300": 8,
    "minimum_source_audit_sample": 40,
    "minimum_audited_cue_spans": 20,
    "minimum_supported_audit_fraction": 0.90,
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _private_dir(path: Path) -> Path:
    path = Path(path).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def _write_private_json(path: Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(path, 0o600)


def _protocol_payload(amendment: dict | None = None) -> dict:
    cues = json.dumps(EXPERIENCE_CUES, sort_keys=True, separators=(",", ":"))
    return {
        "version": PROTOCOL_VERSION,
        "study": "reading_experience_source_quality_and_causal_screen",
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
        "script_sha256": sha256_file(Path(__file__)),
        "amendment": {
            "supersedes_script_sha256": PREVIOUS_SCRIPT_SHA256,
            "reason": "Corrected subject-field provenance hashing to the production 12-subject limit while retaining the 8-subject feature cap; original sample IDs, cues, source rules, folds, and advancement gate are unchanged.",
            "ratings_opened_before_amendment": False,
        },
        "schema_sha256": hashlib.sha256(cues.encode("utf-8")).hexdigest(),
        "source_rules": {
            "description": "Only synopsis descriptions that pass _verified_field_provenance identity, value-hash, kind, provider, and field-row checks.",
            "extractor": "Fixed exact phrase cues listed in this protocol; spans must occur in the verified source synopsis.",
            "subject_value_hash_normalization_limit": grounded.METADATA_GENRE_LIMIT,
            "subject_feature_limit": grounded.FACET_SUBJECT_FEATURE_LIMIT,
            "subject_limit_scope": "Verify catalog subject provenance using the production 12-subject normalization; retain the existing eight-subject feature cap.",
            "forbidden_inputs": ["title", "rating", "review", "model memory", "generated description"],
            "unknown": "A family without an explicit supported cue has mask 0 and contributes no positive or negative feature.",
        },
        "pilot": {
            "sample_size": TARGET_SAMPLE_SIZE,
            "sample_seed": SAMPLE_SEED,
            "strata": ["rich_fiction", "rich_nonfiction", "sparse_fiction", "sparse_nonfiction", "unknown_genre", "no_verified_synopsis"],
            "per_stratum_target": 10,
            "sample_unit": "eligible distinct read id from the frozen feature manifest",
            "label_blind": True,
            "evidence_archive": "private; includes verified source reference, description hash, exact spans, and source text for audit",
            "amended_sample_selection": "When a correction amendment is present, reuse the original frozen sample IDs and record their original sampling strata separately from corrected 12-subject classifications.",
        },
        "advance_gate": PILOT_COVERAGE_GATE,
        "causal_screen": {
            "eligible_rows": "all frozen causal feature targets; no coverage-based row exclusion",
            "folds": "frozen whole-day expanding development folds; same rows as current ranker replay",
            "arms": ["current", "appeal_only", "current_plus_topic", "current_plus_appeal", "current_plus_topic_and_appeal"],
            "topic_control": "verified catalog subjects, fixed synopsis topics, fixed setting-time cues, and group-known masks; subject vocabulary fit on each training prefix",
            "experience_features": "fixed cue indicators plus four family-known masks",
            "learner": f"Ridge alpha={RIDGE_ALPHA}; training-only fit, no tuning",
            "residual": "fit ratings minus training-only Ridge calibration of the current score; multiply predicted star residual by 25 points and clip by min(5, 0.25 * training current-score SD)",
            "appeal_only": "Ridge prediction mapped from 1-5 stars to 0-100; rows with no supported experience cue use the current score",
            "metrics": ["balanced high/low AUC", "top-8 and top-20 favorite precision", "top-8 and top-20 disliked inclusion", "NDCG@8 and NDCG@20", "paired author/day bootstrap"],
            "selection": "No hyperparameter search and no promotion selection; report fixed arms as exploratory screens only.",
        },
        "limitations": [
            "Single-reader historical ratings have been inspected in prior studies.",
            "Current synopsis availability does not prove historical availability at read time.",
            "Eventually-read books form a synthetic pool, not actual recommendation slates.",
        ],
        "correction_amendment": amendment,
    }


def _freeze_correction_amendment(output_dir: Path, prior_dir: Path) -> dict:
    """Freeze a provenance-limit correction and the original label-blind sample."""
    prior_dir = Path(prior_dir).expanduser().resolve()
    protocol_path = prior_dir / "experience-protocol-private.json"
    report_path = prior_dir / "experience-pilot-report-private.json"
    evidence_path = prior_dir / "experience-pilot-evidence-private.json"
    for path in (protocol_path, report_path, evidence_path):
        if not path.is_file():
            raise ValueError("Prior frozen experience pilot is missing a required artifact")
    old_protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    old_report = json.loads(report_path.read_text(encoding="utf-8"))
    old_evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    old_pilot = old_report.get("pilot", {})
    rows = old_evidence.get("rows", [])
    if old_report.get("protocol_sha256") != sha256_file(protocol_path):
        raise ValueError("Prior experience report does not match its frozen protocol")
    if old_protocol.get("pilot", {}).get("label_blind") is not True:
        raise ValueError("Prior experience sample was not label-blind")
    if old_pilot.get("labels_accessed") is not False or old_report.get("appeal_model_fitted") is not False:
        raise ValueError("Prior experience pilot is not eligible for a label-blind correction")
    if old_report.get("coverage_gate", {}).get("passed") is not False:
        raise ValueError("Prior experience gate passed; this correction path must be reviewed separately")
    if len(rows) != TARGET_SAMPLE_SIZE:
        raise ValueError("Prior experience evidence does not contain the frozen 60-row sample")
    sample_ids = [int(row["read_id"]) for row in rows]
    if len(set(sample_ids)) != TARGET_SAMPLE_SIZE:
        raise ValueError("Prior experience sample contains duplicate IDs")
    historical_strata = [str(row["stratum"]) for row in rows]
    sample_ids_sha256 = hashlib.sha256(json.dumps(
        sample_ids, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    lock = {
        "source_protocol_sha256": sha256_file(protocol_path),
        "source_report_sha256": sha256_file(report_path),
        "source_evidence_sha256": sha256_file(evidence_path),
        "sample_ids_sha256": sample_ids_sha256,
        "rows": [
            {"read_id": read_id, "original_sampling_stratum": stratum,
             "sample_position": position}
            for position, (read_id, stratum) in enumerate(zip(sample_ids, historical_strata))
        ],
    }
    lock_path = Path(output_dir) / "experience-frozen-sample-lock-private.json"
    _write_private_json(lock_path, lock)
    amendment = {
        "amendment_version": 1,
        "status": "frozen_before_corrected_outputs",
        "reason": "Correct genre-field provenance hashing from the evaluator default of eight to production's twelve; keep the eight-subject feature cap, source cue lexicon, gates, folds, and sample IDs unchanged.",
        "ratings_opened_before_amendment": False,
        "prior_protocol_sha256": sha256_file(protocol_path),
        "prior_report_sha256": sha256_file(report_path),
        "prior_evidence_sha256": sha256_file(evidence_path),
        "frozen_sample_lock_sha256": sha256_file(lock_path),
        "sample_size": TARGET_SAMPLE_SIZE,
        "sample_ids_sha256": sample_ids_sha256,
        "eligible_feature_sha256": old_pilot.get("feature_manifest_sha256"),
        "corpus_sha256": old_pilot.get("corpus_sha256"),
        "provenance_sha256": old_pilot.get("provenance_sha256"),
        "cue_schema_sha256": old_protocol.get("schema_sha256"),
        "source_gate": old_protocol.get("advance_gate"),
        "subject_value_hash_normalization_limit": grounded.METADATA_GENRE_LIMIT,
        "subject_feature_limit": grounded.FACET_SUBJECT_FEATURE_LIMIT,
        "selection_strata": "Original IDs and original strata are inherited as frozen; corrected 12-subject strata are descriptive classifications only and do not trigger resampling.",
        "evaluator_script_sha256": sha256_file(Path(__file__)),
    }
    amendment_path = Path(output_dir) / "experience-protocol-amendment-private.json"
    _write_private_json(amendment_path, amendment)
    amendment["amendment_file_sha256"] = sha256_file(amendment_path)
    return amendment


def freeze_protocol(output_dir: Path, correction_from: Path | None = None) -> dict:
    output_dir = _private_dir(output_dir)
    path = output_dir / "experience-protocol-private.json"
    amendment = None
    if correction_from is not None:
        amendment_path = output_dir / "experience-protocol-amendment-private.json"
        if path.exists() or amendment_path.exists():
            if not (path.exists() and amendment_path.exists()):
                raise ValueError("Correction freeze is incomplete; preserve the directory and inspect it")
            amendment = json.loads(amendment_path.read_text(encoding="utf-8"))
            if amendment.get("evaluator_script_sha256") != sha256_file(Path(__file__)):
                raise ValueError("Frozen correction amendment was created by a different evaluator")
            amendment["amendment_file_sha256"] = sha256_file(amendment_path)
        else:
            amendment = _freeze_correction_amendment(output_dir, correction_from)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("script_sha256") != sha256_file(Path(__file__)):
            raise ValueError("Frozen protocol was created by a different evaluator version")
        if existing.get("schema_sha256") != _protocol_payload(amendment)["schema_sha256"]:
            raise ValueError("Frozen experience cue schema differs")
        if existing.get("correction_amendment") != amendment:
            raise ValueError("Frozen experience protocol has a different correction amendment")
        return existing
    payload = _protocol_payload(amendment)
    _write_private_json(path, payload)
    return payload


def _require_protocol(output_dir: Path) -> dict:
    path = Path(output_dir) / "experience-protocol-private.json"
    if not path.exists():
        raise ValueError("Freeze the experience protocol before reading source or rating data")
    protocol = json.loads(path.read_text(encoding="utf-8"))
    if protocol.get("script_sha256") != sha256_file(Path(__file__)):
        raise ValueError("Evaluator source changed after the experience protocol was frozen")
    amendment = protocol.get("correction_amendment")
    if protocol.get("schema_sha256") != _protocol_payload(amendment)["schema_sha256"]:
        raise ValueError("Frozen experience cue schema differs")
    if amendment is not None:
        amendment_path = Path(output_dir) / "experience-protocol-amendment-private.json"
        lock_path = Path(output_dir) / "experience-frozen-sample-lock-private.json"
        if not amendment_path.is_file() or not lock_path.is_file():
            raise ValueError("Frozen correction amendment or inherited sample lock is missing")
        if sha256_file(amendment_path) != amendment.get("amendment_file_sha256"):
            raise ValueError("Frozen correction amendment file hash differs")
        if sha256_file(lock_path) != amendment.get("frozen_sample_lock_sha256"):
            raise ValueError("Frozen sample lock differs from the amendment")
    return protocol


def _verified_metadata(corpus: dict, supplement: dict, eligible_ids: np.ndarray) -> dict[int, dict]:
    reads = {int(row["id"]): row for row in corpus.get("reads", [])}
    originals = {int(row["read_id"]): row for row in corpus.get("read_metadata", [])}
    supplements = {int(row["read_id"]): row for row in supplement.get("read_metadata", [])}
    field_rows: dict[int, list[dict]] = {}
    for row in supplement.get("metadata_field_provenance", []):
        if row.get("entity_type") == "read":
            field_rows.setdefault(int(row["entity_id"]), []).append(row)
    if set(originals) != set(supplements):
        raise ValueError("Frozen metadata and provenance supplement do not align")
    result = {}
    for read_id_value in eligible_ids:
        read_id = int(read_id_value)
        read, original, supplemented = reads.get(read_id), originals.get(read_id), supplements.get(read_id)
        if read is None:
            raise ValueError("Eligible feature read id is missing from corpus")
        if original is not None and supplemented is not None:
            if any(supplemented.get(key) != value for key, value in original.items()):
                raise ValueError("Frozen metadata values differ from provenance supplement")
        rows = field_rows.get(read_id, [])
        description_provenance = grounded._verified_field_provenance(
            read, original, supplemented, rows, "description")
        subjects_provenance = grounded._verified_field_provenance(
            read, original, supplemented, rows, "genres")
        result[read_id] = {
            "read": {key: read.get(key) for key in ("id", "title", "author", "isbn")},
            "original": original,
            "description_provenance": description_provenance,
            "subjects_provenance": subjects_provenance,
        }
    return result


def _is_negated(text: str, start: int) -> bool:
    prefix = text[max(0, start - 50):start].casefold()
    return re.search(r"\b(?:not|never|no|without|lacks?)\b(?:\W+\w+){0,3}\W*$", prefix) is not None


def extract_experience(description: str | None, provenance: dict | None) -> dict:
    """Extract fixed, literal cues from a verified synopsis only."""
    text = str(description or "")
    if (
        not provenance
        or provenance.get("kind") != "synopsis"
        or not provenance.get("source")
        or not provenance.get("source_reference")
        or hashlib.sha256(text.encode("utf-8")).hexdigest() != provenance.get("document_sha256")
    ):
        text = ""
        provenance = {}
    values = {name: 0 for name in EXPERIENCE_CUES}
    evidence = {name: [] for name in EXPERIENCE_CUES}
    for feature, phrases in EXPERIENCE_CUES.items():
        for phrase in phrases:
            pattern = re.compile(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", re.IGNORECASE)
            for match in pattern.finditer(text):
                if _is_negated(text, match.start()):
                    continue
                values[feature] = 1
                evidence[feature].append({
                    "source": provenance.get("source"),
                    "source_reference": provenance.get("source_reference"),
                    "source_field": provenance.get("source_field", "description"),
                    "document_sha256": provenance.get("document_sha256"),
                    "span": [match.start(), match.end()],
                    "evidence_text": text[match.start():match.end()],
                })
                if len(evidence[feature]) >= 3:
                    break
            if len(evidence[feature]) >= 3:
                break
    known = {
        group: int(any(values[name] for name in names))
        for group, names in EXPERIENCE_GROUPS.items()
    }
    return {"values": values, "known": known, "evidence": evidence}


def _stratum(meta: dict, extracted: dict) -> str:
    description_known = bool(meta.get("description_provenance"))
    subjects_known = bool(meta.get("subjects_provenance"))
    original = meta.get("original") or {}
    # Classification follows the production provenance hash coverage. This
    # affects descriptive strata only; modeled subject features stay capped at 8.
    genres = normalize_subjects(
        original.get("genres"), limit=grounded.METADATA_GENRE_LIMIT
    ) if subjects_known else []
    normalized = {str(value).strip().casefold().replace("-", " ") for value in genres}
    fiction = "fiction" in normalized
    nonfiction = "nonfiction" in normalized or "non fiction" in normalized
    genre = "fiction" if fiction and not nonfiction else "nonfiction" if nonfiction and not fiction else "unknown"
    if not description_known:
        return "no_verified_synopsis"
    richness = "rich" if subjects_known else "sparse"
    return f"{richness}_{genre}" if genre != "unknown" else "unknown_genre"


def _sample_ids(rows: dict[int, dict], n: int = TARGET_SAMPLE_SIZE) -> list[int]:
    ordered_strata = (
        "rich_fiction", "rich_nonfiction", "sparse_fiction", "sparse_nonfiction",
        "unknown_genre", "no_verified_synopsis",
    )
    pools = {name: [] for name in ordered_strata}
    for read_id, item in rows.items():
        pools[item["stratum"]].append(read_id)
    rank = lambda read_id: hashlib.sha256(f"{SAMPLE_SEED}:{read_id}".encode()).hexdigest()
    selected: list[int] = []
    for name in ordered_strata:
        selected.extend(sorted(pools[name], key=rank)[:10])
    selected_set = set(selected)
    if len(selected) < min(n, len(rows)):
        remainder = sorted((value for value in rows if value not in selected_set), key=rank)
        selected.extend(remainder[:min(n, len(rows)) - len(selected)])
    return selected[:n]


def _write_disabled_score_archive(features_path: Path, output_dir: Path) -> Path:
    """Write ID-aligned current fallback arrays without opening rating labels."""
    with np.load(features_path, allow_pickle=False) as archive:
        arrays = {name: archive[name].copy() for name in (
            "read_ids", "current", "train_mask", "validation_mask", "later_mask",
            "oof_selection_mask",
        )}
    path = Path(output_dir) / "experience-disabled-scores-private.npz"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        np.savez_compressed(
            stream,
            **arrays,
            score__current=arrays["current"],
            score__experience_disabled_fallback=arrays["current"],
        )
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(path, 0o600)
    return path


def _load_labelblind_inputs(corpus_path: Path, features_path: Path, manifest_path: Path,
                            provenance_path: Path):
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if sha256_file(features_path) != manifest.get("feature_artifact_sha256"):
        raise ValueError("Feature archive hash differs from its frozen manifest")
    with np.load(features_path, allow_pickle=False) as archive:
        # Deliberately access only IDs; no outcome array is opened in the pilot.
        eligible_ids = archive["read_ids"].copy()
    corpus = json.loads(Path(corpus_path).read_text(encoding="utf-8"))
    supplement = json.loads(Path(provenance_path).read_text(encoding="utf-8"))
    return manifest, eligible_ids, _verified_metadata(corpus, supplement, eligible_ids)


def run_pilot(*, corpus_path: Path, features_path: Path, manifest_path: Path,
              provenance_path: Path, output_dir: Path) -> dict:
    protocol = _require_protocol(output_dir)
    manifest, eligible_ids, metadata = _load_labelblind_inputs(
        corpus_path, features_path, manifest_path, provenance_path)
    rows = {}
    group_counts = {name: 0 for name in EXPERIENCE_GROUPS}
    prefix_ids = set(map(int, eligible_ids[:300]))
    prefix_group_counts = {name: 0 for name in EXPERIENCE_GROUPS}
    any_count = 0
    source_verified = subject_verified = 0
    stratum_counts: dict[str, int] = {}
    all_evidence = {}
    for read_id_value in eligible_ids:
        read_id = int(read_id_value)
        item = metadata[read_id]
        original = item.get("original") or {}
        description = original.get("description")
        extracted = extract_experience(description, item.get("description_provenance"))
        stratum = _stratum(item, extracted)
        item["stratum"] = stratum
        rows[read_id] = item
        stratum_counts[stratum] = stratum_counts.get(stratum, 0) + 1
        source_verified += bool(item.get("description_provenance"))
        subject_verified += bool(item.get("subjects_provenance"))
        group_count = sum(extracted["known"].values())
        any_count += group_count > 0
        for group, known in extracted["known"].items():
            group_counts[group] += known
            prefix_group_counts[group] += int(read_id in prefix_ids and known)
        all_evidence[read_id] = extracted

    amendment = protocol.get("correction_amendment")
    original_sampling_strata: dict[int, str] = {}
    if amendment is not None:
        lock_path = Path(output_dir) / "experience-frozen-sample-lock-private.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        locked_rows = lock.get("rows", [])
        sample_ids = [int(row["read_id"]) for row in locked_rows]
        if len(sample_ids) != TARGET_SAMPLE_SIZE or len(set(sample_ids)) != TARGET_SAMPLE_SIZE:
            raise ValueError("Amended pilot sample lock must contain the original 60 unique IDs")
        if hashlib.sha256(json.dumps(
            sample_ids, separators=(",", ":")
        ).encode("utf-8")).hexdigest() != amendment.get("sample_ids_sha256"):
            raise ValueError("Amended pilot sample IDs differ from the frozen selection")
        if not set(sample_ids).issubset(rows):
            raise ValueError("Frozen sample contains an ID outside the corrected eligible cohort")
        original_sampling_strata = {
            int(row["read_id"]): str(row["original_sampling_stratum"])
            for row in locked_rows
        }
    else:
        sample_ids = _sample_ids(rows)
    evidence_rows = []
    sample_cue_spans = 0
    for read_id in sample_ids:
        item = rows[read_id]
        original = item.get("original") or {}
        extracted = all_evidence[read_id]
        desc = str(original.get("description") or "") if item.get("description_provenance") else ""
        sample_cue_spans += sum(len(spans) for spans in extracted["evidence"].values())
        evidence_rows.append({
            "read_id": read_id,
            "stratum": item["stratum"],
            **({"original_sampling_stratum": original_sampling_strata[read_id]}
               if original_sampling_strata else {}),
            "description_provenance": item.get("description_provenance"),
            "subjects_provenance": item.get("subjects_provenance"),
            "description_sha256": hashlib.sha256(desc.encode("utf-8")).hexdigest() if desc else None,
            "description_source_text": desc,
            "experience": extracted,
        })
    output_dir = _private_dir(output_dir)
    disabled_scores_path = _write_disabled_score_archive(features_path, output_dir)
    evidence_path = output_dir / "experience-pilot-evidence-private.json"
    _write_private_json(evidence_path, {"rows": evidence_rows})
    counts = {
        "eligible_targets": int(len(eligible_ids)),
        "verified_synopsis_count": int(source_verified),
        "verified_subject_count": int(subject_verified),
        "strata": stratum_counts,
        "stratum_classification_limit": grounded.METADATA_GENRE_LIMIT,
        "sampling_selection": (
            "original frozen IDs and original strata retained; corrected 12-subject strata are descriptive only"
            if amendment is not None else "strata from this frozen protocol"
        ),
        **({"original_sampling_strata": {
            name: sum(value == name for value in original_sampling_strata.values())
            for name in sorted(set(original_sampling_strata.values()))
        }} if original_sampling_strata else {}),
        "sampled_rows": len(sample_ids),
        "sampled_rows_with_verified_synopsis": sum(bool(rows[i].get("description_provenance")) for i in sample_ids),
        "sampled_rows_with_cue": sum(any(all_evidence[i]["known"].values()) for i in sample_ids),
        "sampled_literal_cue_spans": int(sample_cue_spans),
        "any_supported_experience_cue": int(any_count),
        "experience_group_coverage": group_counts,
        "experience_group_coverage_initial_300": prefix_group_counts,
        "unknown_policy": "No matched literal cue leaves the family unknown; no negative experience is imputed.",
        "labels_accessed": False,
        "feature_manifest_sha256": sha256_file(features_path),
        "corpus_sha256": sha256_file(corpus_path),
        "provenance_sha256": sha256_file(provenance_path),
        "evidence_archive_sha256": sha256_file(evidence_path),
        "disabled_scores_sha256": sha256_file(disabled_scores_path),
    }
    gate = coverage_gate(counts, PILOT_COVERAGE_GATE)
    report = {
        "protocol_sha256": sha256_file(output_dir / "experience-protocol-private.json"),
        "correction_amendment_sha256": (
            amendment.get("amendment_file_sha256") if amendment is not None else None
        ),
        "script_sha256": protocol["script_sha256"],
        "pilot": counts,
        "coverage_gate": gate,
        "next_step": "manual_source_audit_required_before_any_rating_screen" if gate["passed"] else "stop; source coverage did not meet the preregistered minimum",
        "evidence_file": evidence_path.name,
        "aligned_disabled_scores_file": disabled_scores_path.name,
        "aligned_disabled_score_ids": int(len(eligible_ids)),
        "appeal_model_fitted": False,
        "appeal_component_status": "disabled_by_preregistered_source_coverage_gate",
    }
    _write_private_json(output_dir / "experience-pilot-report-private.json", report)
    print(json.dumps({"pilot": counts, "coverage_gate": gate, "next_step": report["next_step"]}, indent=2))
    return report


def coverage_gate(counts: dict, rules: dict = PILOT_COVERAGE_GATE) -> dict:
    group_coverage = counts["experience_group_coverage"]
    prefix_coverage = counts["experience_group_coverage_initial_300"]
    supported_groups = [name for name, value in group_coverage.items()
                        if value >= rules["minimum_supported_per_group"]]
    checks = {
        "minimum_any_supported_cue": counts["any_supported_experience_cue"] >= rules["minimum_any_supported_cue"],
        "minimum_supported_groups": len(supported_groups) >= rules["minimum_supported_groups"],
        "minimum_initial_prefix_support": all(
            prefix_coverage[name] >= rules["minimum_per_group_in_initial_300"]
            for name in supported_groups
        ) if supported_groups else False,
    }
    return {"passed": all(checks.values()), "checks": checks,
            "supported_groups": supported_groups, "rules": rules}


def _experience_matrix(extracted: list[dict]) -> np.ndarray:
    names = list(EXPERIENCE_CUES)
    groups = list(EXPERIENCE_GROUPS)
    matrix = np.zeros((len(extracted), len(names) + len(groups)), dtype=np.float64)
    for row, item in enumerate(extracted):
        for col, name in enumerate(names):
            matrix[row, col] = item["values"][name]
        for offset, group in enumerate(groups):
            matrix[row, len(names) + offset] = item["known"][group]
    return matrix


def _topic_matrix(facets: list[dict], train_indexes: np.ndarray) -> tuple[np.ndarray, np.ndarray, dict]:
    groups = ("catalog_subjects", "synopsis_topics", "setting_time")
    label_sets = {name: [] for name in groups}
    for facet in facets:
        values = facet.get("values", {})
        for name in groups:
            value = values.get(name, "unknown")
            label_sets[name].append(set(value) if isinstance(value, list) else set())
    subject_vocab = sorted(set().union(*(label_sets["catalog_subjects"][i] for i in train_indexes)))
    fixed_vocab = {
        "catalog_subjects": subject_vocab,
        "synopsis_topics": sorted(grounded.TOPIC_CUES),
        "setting_time": sorted(grounded.TIME_CUES),
    }
    columns = []
    matrices = []
    group_known = []
    for group in groups:
        vocab = fixed_vocab[group]
        values = np.zeros((len(facets), len(vocab)), dtype=np.float64)
        position = {name: col for col, name in enumerate(vocab)}
        known = np.zeros(len(facets), dtype=np.float64)
        for row, labels in enumerate(label_sets[group]):
            known[row] = bool(labels)
            for label in labels:
                if label in position:
                    values[row, position[label]] = 1.0
        matrices.extend([values, known[:, None]])
        group_known.append(known)
        columns.extend([*(f"{group}:{name}" for name in vocab), f"{group}:known"])
    return (
        np.concatenate(matrices, axis=1),
        np.stack(group_known, axis=1),
        {"feature_count": len(columns), "columns": columns,
         "training_subject_vocabulary_size": len(subject_vocab)},
    )


def _bounded_residual(train: np.ndarray, evaluate: np.ndarray, current: np.ndarray,
                      ratings: np.ndarray, feature_matrix: np.ndarray,
                      known: np.ndarray) -> np.ndarray:
    train = np.asarray(train, dtype=np.int64)
    evaluate = np.asarray(evaluate, dtype=np.int64)
    current_train = current[train]
    current_mean, current_sd = float(current_train.mean()), float(current_train.std())
    if current_sd <= 1e-12:
        cap = 0.0
        current_sd = 1.0
    else:
        cap = min(MAX_CORRECTION_POINTS, CORRECTION_SD_FRACTION * current_sd)
    current_model = Ridge(alpha=RIDGE_ALPHA).fit(
        ((current_train - current_mean) / current_sd).reshape(-1, 1), ratings[train]
    )
    calibrated_train = current_model.predict(
        ((current_train - current_mean) / current_sd).reshape(-1, 1)
    )
    residual = ratings[train] - calibrated_train
    residual_model = Ridge(alpha=RIDGE_ALPHA).fit(feature_matrix[train], residual)
    correction = RATING_SCALE * residual_model.predict(feature_matrix[evaluate])
    correction = np.clip(correction, -cap, cap)
    correction[~known[evaluate]] = 0.0
    result = np.clip(current[evaluate] + correction, 0.0, 100.0)
    return np.fromiter((round(float(value), 1) for value in result), dtype=np.float64, count=len(result))


def _appeal_only(train: np.ndarray, evaluate: np.ndarray, ratings: np.ndarray,
                 feature_matrix: np.ndarray, known: np.ndarray, current: np.ndarray) -> np.ndarray:
    model = Ridge(alpha=RIDGE_ALPHA).fit(feature_matrix[train], ratings[train])
    prediction = RATING_SCALE * (model.predict(feature_matrix[evaluate]) - 1.0)
    prediction = np.clip(prediction, 0.0, 100.0)
    prediction[~known[evaluate]] = current[evaluate][~known[evaluate]]
    return np.fromiter((round(float(value), 1) for value in prediction), dtype=np.float64, count=len(prediction))


def _metric_row(ratings: np.ndarray, scores: np.ndarray, ids: np.ndarray,
                current: np.ndarray | None = None) -> dict:
    result = metrics.metrics(ratings, scores)
    order = np.lexsort((ids, -scores))
    for k in (8, 20):
        chosen = order[:min(k, len(order))]
        result[f"top{k}_high_precision"] = float((ratings[chosen] >= 4).mean())
        result[f"top{k}_dislike_inclusion"] = float((ratings[chosen] <= 2).mean())
        gain = 2.0 ** (ratings - 1.0) - 1.0
        discount = 1.0 / np.log2(np.arange(len(chosen)) + 2.0)
        ideal = np.sort(gain)[::-1][:len(chosen)] @ discount
        result[f"ndcg{k}"] = float(gain[chosen] @ discount / ideal) if ideal else 0.0
        if current is not None:
            base_order = np.lexsort((ids, -current))[:min(k, len(order))]
            result[f"top{k}_baseline_overlap"] = len(set(ids[chosen]) & set(ids[base_order]))
    return result


def run_compare(*, corpus_path: Path, features_path: Path, manifest_path: Path,
                provenance_path: Path, output_dir: Path) -> dict:
    protocol = _require_protocol(output_dir)
    pilot_path = Path(output_dir) / "experience-pilot-report-private.json"
    if not pilot_path.exists():
        raise ValueError("Run the label-blind source pilot before the rating screen")
    pilot = json.loads(pilot_path.read_text(encoding="utf-8"))
    if not pilot.get("coverage_gate", {}).get("passed"):
        raise ValueError("Source pilot did not meet the frozen coverage gate")
    audit_path = Path(output_dir) / "experience-pilot-audit-private.json"
    if not audit_path.exists():
        raise ValueError("Complete the blinded source-text audit before reading rating outcomes")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    gate = pilot["coverage_gate"]["rules"]
    if audit.get("sampled_rows_audited", 0) < gate["minimum_source_audit_sample"]:
        raise ValueError("Source-text audit does not cover the frozen minimum sample")
    if audit.get("audited_cue_spans", 0) < gate["minimum_audited_cue_spans"]:
        raise ValueError("Source-text audit does not cover the frozen minimum cue count")
    if audit.get("supported_fraction", 0.0) < gate["minimum_supported_audit_fraction"]:
        raise ValueError("Source-text audit did not meet the frozen support fraction")
    if sha256_file(corpus_path) != pilot["pilot"]["corpus_sha256"]:
        raise ValueError("Corpus differs from the label-blind source pilot")
    if sha256_file(features_path) != pilot["pilot"]["feature_manifest_sha256"]:
        raise ValueError("Feature archive differs from the label-blind source pilot")
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if sha256_file(features_path) != manifest.get("feature_artifact_sha256"):
        raise ValueError("Feature archive hash differs from its frozen manifest")
    import evaluate_ranking

    corpus = evaluate_ranking.load_corpus(Path(corpus_path))
    supplement = json.loads(Path(provenance_path).read_text(encoding="utf-8"))
    with np.load(features_path, allow_pickle=False) as archive:
        feature_arrays = {key: archive[key].copy() for key in archive.files}
    ratings = np.asarray(feature_arrays["ratings"], dtype=np.float64)
    current = np.asarray(feature_arrays["current"], dtype=np.float64)
    read_ids = np.asarray(feature_arrays["read_ids"], dtype=np.int64)
    if not np.isfinite(ratings).all() or not np.isfinite(current).all():
        raise ValueError("Frozen causal labels or current scores are malformed")
    eligible_set = set(map(int, read_ids))
    metadata = _verified_metadata(corpus, supplement, read_ids)
    records, selected_encoder, _ = evaluate_ranking.prepare(
        corpus, *manifest["backend_model"]
    )
    record_indexes = np.asarray(feature_arrays["record_indexes"], dtype=np.int64)
    if not np.array_equal(read_ids, [int(records[index][1]["id"]) for index in record_indexes]):
        raise ValueError("Causal feature archive is not aligned to prepared records")
    if list(selected_encoder) != manifest["backend_model"]:
        raise ValueError("Prepared corpus selected a different embedding model")
    if sha256_file(provenance_path) != pilot["pilot"]["provenance_sha256"]:
        raise ValueError("Provenance supplement differs from the source pilot")

    experience_rows = []
    topic_facets = []
    for read_id_value in read_ids:
        read_id = int(read_id_value)
        item = metadata[read_id]
        original = item.get("original") or {}
        description = original.get("description")
        extracted = extract_experience(description, item.get("description_provenance"))
        experience_rows.append(extracted)
        topic_facets.append(grounded.extract_grounded_facets(
            description=description,
            description_provenance=item.get("description_provenance"),
            subjects=(
                normalize_subjects(
                    original.get("genres"),
                    limit=grounded.FACET_SUBJECT_FEATURE_LIMIT,
                )
                if item.get("subjects_provenance") else None
            ),
            subjects_provenance=item.get("subjects_provenance"),
        ))
    experience = _experience_matrix(experience_rows)
    experience_known = np.asarray([any(item["known"].values()) for item in experience_rows], dtype=bool)

    count = len(read_ids)
    scores = {name: np.full(count, np.nan, dtype=np.float64) for name in (
        "appeal_only", "current_plus_topic", "current_plus_appeal",
        "current_plus_topic_and_appeal",
    )}
    fold_specs = [(int(row["train_stop"]), int(row["evaluation_start"]), int(row["evaluation_stop"]))
                  for row in manifest["development_folds"]]
    train_stop = int(feature_arrays["train_mask"].sum())
    validation_stop = train_stop + int(feature_arrays["validation_mask"].sum())
    fold_specs.extend([(train_stop, train_stop, validation_stop),
                       (validation_stop, validation_stop, count)])
    summaries = []
    for fold_index, (fit_stop, begin, end) in enumerate(fold_specs):
        if not (0 < fit_stop == begin < end <= count):
            raise ValueError("Frozen causal fold bounds are invalid")
        train = np.arange(fit_stop, dtype=np.int64)
        evaluate = np.arange(begin, end, dtype=np.int64)
        topic, topic_group_known, topic_summary = _topic_matrix(topic_facets, train)
        topic_known = topic_group_known.any(axis=1)
        combined = np.concatenate((topic, experience), axis=1)
        combined_known = topic_known | experience_known
        for name, values, known in (
            ("appeal_only", experience, experience_known),
            ("current_plus_topic", topic, topic_known),
            ("current_plus_appeal", experience, experience_known),
            ("current_plus_topic_and_appeal", combined, combined_known),
        ):
            if name == "appeal_only":
                prediction = _appeal_only(train, evaluate, ratings, values, known, current)
            else:
                prediction = _bounded_residual(train, evaluate, current, ratings, values, known)
            scores[name][begin:end] = prediction
        summaries.append({
            "fold": fold_index + 1,
            "train_rows": int(fit_stop),
            "evaluation_rows": int(end - begin),
            "topic_features": topic_summary["feature_count"],
            "training_subject_vocabulary_size": topic_summary["training_subject_vocabulary_size"],
            "training_experience_rows": int(experience_known[train].sum()),
            "evaluation_experience_rows": int(experience_known[evaluate].sum()),
        })

    arm_scores = {"current": current, **scores}
    sections = {}
    for label, indexes in (
        ("development_oof", np.concatenate([
            np.arange(begin, end) for _, begin, end in fold_specs[:3]
        ])),
        ("validation", np.flatnonzero(feature_arrays["validation_mask"])),
        ("later_exploratory", np.flatnonzero(feature_arrays["later_mask"])),
    ):
        sections[label] = {
            name: _metric_row(ratings[indexes], values[indexes], read_ids[indexes], current[indexes])
            for name, values in arm_scores.items()
        }
    uncertainty = {}
    for split_name, mask_name in (("validation", "validation_mask"), ("later_exploratory", "later_mask")):
        indexes = np.flatnonzero(feature_arrays[mask_name])
        uncertainty[split_name] = {}
        for arm, values in scores.items():
            uncertainty[split_name][arm] = {
                group: metrics.grouped_bootstrap(
                    ratings[indexes], current[indexes], values[indexes], feature_arrays[group][indexes],
                    repetitions=1000, seed=20261002,
                )
                for group in ("utc_day", "author_group")
            }
    score_path = Path(output_dir) / "experience-scores-private.npz"
    descriptor = os.open(score_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        np.savez_compressed(stream, read_ids=read_ids, **{"score__" + name: value
                                                          for name, value in arm_scores.items()})
        stream.flush()
        os.fsync(stream.fileno())
    report = {
        "protocol_sha256": sha256_file(Path(output_dir) / "experience-protocol-private.json"),
        "pilot_report_sha256": sha256_file(pilot_path),
        "audit_sha256": sha256_file(audit_path),
        "source_audit": audit,
        "encoder": manifest["backend_model"],
        "eligible_targets": int(count),
        "feature_coverage": {
            "any_supported_experience": int(experience_known.sum()),
            "per_group": {name: int(sum(item["known"][name] for item in experience_rows))
                          for name in EXPERIENCE_GROUPS},
        },
        "folds": summaries,
        "results": sections,
        "paired_group_bootstrap": uncertainty,
        "scores_private": score_path.name,
        "conclusion": "Exploratory only; no scorer promotion. Top-of-list results use synthetic pools of eventually-read books.",
        "limitations": protocol["limitations"],
    }
    _write_private_json(Path(output_dir) / "experience-aggregate-private.json", report)
    print(json.dumps({"feature_coverage": report["feature_coverage"],
                      "results": sections, "conclusion": report["conclusion"]}, indent=2))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze = subparsers.add_parser("freeze")
    freeze.add_argument("--output-dir", type=Path, required=True)
    freeze.add_argument(
        "--prior-dir", type=Path,
        help="Freeze a provenance-limit correction that inherits a prior label-blind pilot sample.",
    )
    for name in ("pilot", "compare"):
        command = subparsers.add_parser(name)
        command.add_argument("--corpus", type=Path, required=True)
        command.add_argument("--features", type=Path, required=True)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--provenance", type=Path, required=True)
        command.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "freeze":
        print(json.dumps(freeze_protocol(args.output_dir, args.prior_dir), indent=2))
    elif args.command == "pilot":
        run_pilot(corpus_path=args.corpus, features_path=args.features,
                  manifest_path=args.manifest, provenance_path=args.provenance,
                  output_dir=args.output_dir)
    else:
        run_compare(corpus_path=args.corpus, features_path=args.features,
                    manifest_path=args.manifest, provenance_path=args.provenance,
                    output_dir=args.output_dir)


if __name__ == "__main__":
    main()
