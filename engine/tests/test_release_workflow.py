from itertools import permutations
import os
from pathlib import Path
import re
import subprocess

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PUBLISH_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "publish.yml"


def _load_workflow(path: Path) -> dict:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _unwrap_gha_expression(raw: str) -> str:
    match = re.fullmatch(r"\$\{\{\s*(.*?)\s*\}\}", raw.strip())
    assert match is not None, f"Expected GitHub Actions expression, got: {raw!r}"
    return match.group(1)


def _evaluate_ci_cancel_in_progress(expr: str, github_ref: str) -> bool:
    """Evaluate simple GitHub Actions boolean concurrency expressions against github.ref."""
    inner = _unwrap_gha_expression(expr)
    clauses = [clause.strip() for clause in inner.split("&&")]
    assert clauses, f"Empty expression: {expr!r}"

    results: list[bool] = []
    for clause in clauses:
        neq_match = re.fullmatch(r"github\.ref\s*!=\s*'([^']*)'", clause)
        if neq_match:
            results.append(github_ref != neq_match.group(1))
            continue

        not_startswith_match = re.fullmatch(
            r"!\s*startsWith\(\s*github\.ref\s*,\s*'([^']*)'\s*\)",
            clause,
        )
        if not_startswith_match:
            prefix = not_startswith_match.group(1).lower()
            results.append(not github_ref.lower().startswith(prefix))
            continue

        raise AssertionError(f"Unsupported CI cancel-in-progress clause: {clause!r}")

    return all(results)


def _render_group(template: str, *, workflow: str, ref: str) -> str:
    return (
        template.replace("${{ github.workflow }}", workflow)
        .replace("${{ github.ref }}", ref)
    )


def _release_step(publish_workflow: dict) -> dict:
    steps = publish_workflow["jobs"]["publish"]["steps"]
    for step in steps:
        if step.get("id") == "release":
            return step
    raise AssertionError("Missing Determine latest release step (id: release)")


def _run_determine_latest_release(repo_dir: Path, release_script: str, release_tag: str) -> str:
    output_file = repo_dir / "github_output.txt"
    output_file.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "RELEASE": release_tag,
        "GITHUB_OUTPUT": str(output_file),
    }
    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", release_script],
        cwd=repo_dir,
        env=env,
        check=True,
    )
    outputs = dict(
        line.split("=", 1)
        for line in output_file.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    return outputs["latest"]


def _init_git_repo(repo_dir: Path, tags: list[str] | tuple[str, ...] = ()) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Bookward Test",
            "-c",
            "user.email=test@bookward.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "initial",
        ],
        cwd=repo_dir,
        check=True,
    )
    for tag in tags:
        _add_git_tag(repo_dir, tag)


def _add_git_tag(repo_dir: Path, tag: str) -> None:
    subprocess.run(
        ["git", "-c", "tag.gpgsign=false", "tag", tag],
        cwd=repo_dir,
        check=True,
    )


def test_ci_workflow_runs_engine_pytest_and_protects_main_and_tag_refs():
    ci = _load_workflow(CI_WORKFLOW_PATH)

    concurrency = ci.get("concurrency")
    assert isinstance(concurrency, dict)
    assert concurrency.get("group") == "ci-${{ github.workflow }}-${{ github.ref }}"

    cancel_expr = concurrency.get("cancel-in-progress")
    assert isinstance(cancel_expr, str)
    assert "refs/heads/main" in cancel_expr
    assert "refs/tags/" in cancel_expr

    assert _evaluate_ci_cancel_in_progress(cancel_expr, "refs/heads/main") is False
    assert _evaluate_ci_cancel_in_progress(cancel_expr, "refs/tags/v1.0.0") is False
    assert _evaluate_ci_cancel_in_progress(cancel_expr, "refs/tags/v2.10.3") is False
    assert _evaluate_ci_cancel_in_progress(cancel_expr, "refs/pull/42/merge") is True
    assert _evaluate_ci_cancel_in_progress(cancel_expr, "refs/heads/feature-x") is True

    engine_steps = ci["jobs"]["engine"]["steps"]
    pytest_commands = [
        step.get("run", "")
        for step in engine_steps
        if "pytest" in step.get("run", "")
    ]
    assert any("engine/tests" in cmd for cmd in pytest_commands)


def test_publish_workflow_serializes_duplicate_tag_runs_and_ghcr_pushes():
    ci = _load_workflow(CI_WORKFLOW_PATH)
    publish = _load_workflow(PUBLISH_WORKFLOW_PATH)

    assert publish.get("permissions") == {"contents": "read"}

    top_concurrency = publish.get("concurrency")
    assert top_concurrency == {
        "group": "publish-${{ github.ref }}",
        "cancel-in-progress": False,
    }

    caller_group = _render_group(
        top_concurrency["group"],
        workflow=publish["name"],
        ref="refs/tags/v1.2.3",
    )
    called_group = _render_group(
        ci["concurrency"]["group"],
        workflow=publish["name"],
        ref="refs/tags/v1.2.3",
    )
    assert caller_group == "publish-refs/tags/v1.2.3"
    assert called_group == "ci-Publish containers-refs/tags/v1.2.3"
    assert caller_group != called_group

    checks_job = publish["jobs"]["checks"]
    assert checks_job.get("needs") == "release-ref"
    assert checks_job.get("uses") == "./.github/workflows/ci.yml"
    assert "concurrency" not in checks_job

    publish_job = publish["jobs"]["publish"]
    assert publish_job.get("needs") == "checks"
    assert publish_job.get("permissions") == {
        "contents": "read",
        "packages": "write",
    }
    assert publish_job.get("concurrency") == {
        "group": "publish-ghcr",
        "cancel-in-progress": False,
        "queue": "max",
    }


def test_publish_job_determines_latest_release_after_lock_with_full_tag_history():
    publish = _load_workflow(PUBLISH_WORKFLOW_PATH)
    steps = publish["jobs"]["publish"]["steps"]

    step_indices = {
        step.get("id") or step.get("name"): idx
        for idx, step in enumerate(steps)
    }

    checkout_idx = step_indices["Check out source"]
    release_idx = step_indices["release"]
    web_meta_idx = step_indices["web-metadata"]
    engine_meta_idx = step_indices["engine-metadata"]
    push_web_idx = step_indices["Publish Bookward image"]
    push_engine_idx = step_indices["Publish engine image"]

    assert checkout_idx < release_idx < web_meta_idx < push_web_idx < push_engine_idx
    assert release_idx < engine_meta_idx < push_engine_idx

    checkout_step = steps[checkout_idx]
    assert checkout_step.get("with", {}).get("fetch-depth") == 0

    for meta_idx in (web_meta_idx, engine_meta_idx):
        tags_cfg = steps[meta_idx]["with"]["tags"]
        assert "steps.release.outputs.latest == 'true'" in tags_cfg


@pytest.mark.parametrize(
    ("existing_tags", "candidate_tag", "expected_latest"),
    [
        (["v1.0.0"], "v1.0.0", "true"),
        (["v1.0.0", "v1.2.0", "v1.10.0"], "v1.10.0", "true"),
        (["v1.0.0", "v1.2.0", "v1.10.0"], "v1.2.0", "false"),
        (["v1.0.0", "v1.0.1", "v1.0.2"], "v1.0.0", "false"),
        (["v1.2.0", "v2.0.0-rc.1", "latest"], "v1.2.0", "true"),
    ],
)
def test_determine_latest_release_script_evaluates_highest_semver_tag(
    tmp_path: Path,
    existing_tags: list[str],
    candidate_tag: str,
    expected_latest: str,
):
    publish = _load_workflow(PUBLISH_WORKFLOW_PATH)
    release_script = _release_step(publish)["run"]

    repo_dir = tmp_path / "repo"
    _init_git_repo(repo_dir, existing_tags)

    assert _run_determine_latest_release(repo_dir, release_script, candidate_tag) == expected_latest


def test_serialized_publish_orders_always_leave_latest_on_highest_release(tmp_path: Path):
    publish = _load_workflow(PUBLISH_WORKFLOW_PATH)
    assert publish["jobs"]["publish"]["concurrency"] == {
        "group": "publish-ghcr",
        "cancel-in-progress": False,
        "queue": "max",
    }
    release_script = _release_step(publish)["run"]
    tags = ("v1.0.0", "v1.0.1", "v1.0.2")

    # Rapid multi-tag push: all three tags exist in git before jobs.publish runs
    # one at a time in any completion order of CI checks.
    all_tags_repo = tmp_path / "all_tags"
    _init_git_repo(all_tags_repo, tags)
    for order in permutations(tags):
        published_versions: list[str] = []
        ghcr_latest: str | None = None
        for tag in order:
            is_latest = _run_determine_latest_release(all_tags_repo, release_script, tag)
            published_versions.append(tag)
            if is_latest == "true":
                ghcr_latest = tag
        assert published_versions == list(order)
        assert ghcr_latest == "v1.0.2"

    # Sequential tag creation + out-of-order backfill: v1.0.0 publishes first,
    # v1.0.2 is tagged and publishes second, then v1.0.1 is backfilled afterward.
    backfill_repo = tmp_path / "backfill"
    _init_git_repo(backfill_repo)
    ghcr_latest = None
    for tag in ("v1.0.0", "v1.0.2", "v1.0.1"):
        _add_git_tag(backfill_repo, tag)
        if _run_determine_latest_release(backfill_repo, release_script, tag) == "true":
            ghcr_latest = tag
    assert ghcr_latest == "v1.0.2"
