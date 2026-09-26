"""Tests for ticket #44: `release.yml` publishes from an orphan `release`
branch, so `--generate-notes` finds no history and the GitHub Release / the
marketplace dispatch's changelog are always empty. The fix generates notes on
`main` between two lightweight `src/<TAG>` markers, via three new scripts
under `.github/scripts/`:

- `prev-release-tag.sh <plugin> <version>` -- prints the greatest existing
  `<plugin>--v*` tag strictly below `<version>` (pure bash SemVer 2.0 §11
  ordering), or nothing (rc 0) for a first release, or exits 2 for an invalid
  `<version>`.
- `release-preflight.sh <plugin> <version>` -- pure `git`, fail-fast: refuses
  to proceed (rc 1, before any side effect) if `src/<TAG>` already exists, if
  the checkout is missing tags `origin` has, or if the resolved predecessor's
  `src/<PREV_TAG>` marker is missing -- printing the exact two bootstrap
  commands a human needs to run once. Never writes to `origin`.
- `marketplace-payload.sh` -- builds the `plugin-release` `client_payload`
  JSON via `jq -n`, reading `CHANGELOG_FILE` and env `NAME/DESC/VERSION/TAG/
  REPO`, writing `PAYLOAD_FILE`; omits the `changelog` key when the body is
  empty/whitespace, truncates by UTF-8 byte length, never ASCII-escapes.

None of the three scripts exist yet in this repo (`.github/scripts/` is not
even a directory) -- every test below is expected to fail RED with rc 127
("No such file or directory") until phase=implement creates them. This is a
genuine "missing behaviour" RED, not a syntax/import/environment failure: the
`run_bash` fixture (tests/conftest.py) itself is proven working by the git
repo builders below succeeding before the script invocation ever runs.

Real git repos are built per test in `tmp_path` (`git init --bare origin.git`
+ a clone, or a plain local repo for prev-release-tag.sh, which never touches
a remote) -- no mocking of git itself, per the plan's test strategy.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / ".github" / "scripts"
PREV_RELEASE_TAG_SCRIPT = SCRIPTS_DIR / "prev-release-tag.sh"
RELEASE_PREFLIGHT_SCRIPT = SCRIPTS_DIR / "release-preflight.sh"
MARKETPLACE_PAYLOAD_SCRIPT = SCRIPTS_DIR / "marketplace-payload.sh"

RELEASE_YML = REPO_ROOT / ".github" / "workflows" / "release.yml"
DISPATCH_YML = REPO_ROOT / ".github" / "workflows" / "dispatch.yml"
FETCH_STEP_NAME = "Fetch changelog for dispatch"
FETCH_STEP_JOB = {RELEASE_YML: "assemble", DISPATCH_YML: "dispatch"}

PLUGIN = "sample-plugin"

TRUNCATE_LIMIT = 30000

HOSTILE_CHANGELOG = (
    "Backticks `like this`, \"double quotes\", and \\backslashes\\.\n"
    "$(rm -rf /)\n"
    "${{ github.token }}\n"
    "EOF\n"
    "trailing line\r\n"
)

DEFAULT_PAYLOAD_ENV = {
    "NAME": "sample-plugin",
    "DESC": "A sample plugin.",
    "VERSION": "0.0.1",
    "TAG": "sample-plugin--v0.0.1",
    "REPO": "seretos-agents/sample-plugin",
}


# ---------------------------------------------------------------------------
# git repo builders -- plain `git` on PATH (not through run_bash/git-bash;
# git.exe is on the normal Windows PATH too), so these never depend on the
# scripts under test.
# ---------------------------------------------------------------------------

def _git(*args, cwd):
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"git {' '.join(args)} failed in {cwd}:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result


def _configure_identity(repo):
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)


def _local_repo_with_version_tags(tmp_path, plugin, versions):
    """A single local repo (no remote at all) with one `<plugin>--v<v>` tag
    per entry in `versions`, all pointing at the same single commit --
    exactly what `prev-release-tag.sh` needs (it never touches a remote)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _configure_identity(repo)
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "init", cwd=repo)
    for version in versions:
        _git("tag", f"{plugin}--v{version}", cwd=repo)
    return repo


def _origin_and_clone(tmp_path, plugin, plugin_versions, src_tag_versions=()):
    """A bare `origin.git` seeded with one commit, one `<plugin>--v<v>` tag
    per `plugin_versions` and one `src/<plugin>--v<v>` tag per
    `src_tag_versions`, all pushed -- plus a real clone of it (what
    `release-preflight.sh` runs against, mirroring an Actions checkout with
    `fetch-depth: 0`). Returns (origin_path, clone_path)."""
    origin = tmp_path / "origin.git"
    _git("init", "--bare", str(origin), cwd=tmp_path)
    # A bare repo's own HEAD symref does not follow whatever branch a later
    # push happens to create -- it stays on git's configured default (e.g.
    # "master") regardless. Point it at "main" explicitly so a `git clone`
    # of this origin checks out a real branch with a resolvable HEAD instead
    # of an unborn one ("remote HEAD refers to nonexistent ref").
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=origin)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-b", "main", cwd=seed)
    _configure_identity(seed)
    _git("remote", "add", "origin", str(origin), cwd=seed)
    (seed / "README.md").write_text("init\n", encoding="utf-8")
    _git("add", "-A", cwd=seed)
    _git("commit", "-m", "init", cwd=seed)
    for version in plugin_versions:
        _git("tag", f"{plugin}--v{version}", cwd=seed)
    for version in src_tag_versions:
        _git("tag", f"src/{plugin}--v{version}", cwd=seed)
    _git("push", "origin", "HEAD:refs/heads/main", cwd=seed)
    _git("push", "origin", "--tags", cwd=seed)

    clone = tmp_path / "clone"
    _git("clone", str(origin), str(clone), cwd=tmp_path)
    _configure_identity(clone)
    return origin, clone


def _ls_remote_tags(tmp_path, origin):
    return _git("ls-remote", "--tags", str(origin), cwd=tmp_path).stdout


# ---------------------------------------------------------------------------
# R1 -- prev-release-tag.sh: predecessor resolution by strict SemVer 2.0 §11
# ordering, over `<plugin>--v*` tags only.
# ---------------------------------------------------------------------------

PREV_TAG_CASES = [
    pytest.param(
        ["0.0.1", "0.0.2", "0.0.3", "0.0.4", "0.0.5", "0.0.6", "0.0.7", "0.0.10"],
        "0.0.11",
        "0.0.10",
        id="numeric-minor-double-digit-gap",
    ),
    pytest.param(
        ["1.0.0-rc.2", "1.0.0-rc.10"],
        "1.0.0",
        "1.0.0-rc.10",
        id="prerelease-numeric-identifier-width-rc2-vs-rc10",
    ),
    pytest.param(
        ["1.0.0-rc.1", "1.0.0"],
        "1.0.1",
        "1.0.0",
        id="release-outranks-its-own-prerelease",
    ),
    pytest.param(
        ["1.0.0-alpha", "1.0.0-1"],
        "1.0.0-beta",
        "1.0.0-alpha",
        id="alphanumeric-identifier-outranks-numeric-identifier",
    ),
]


@pytest.mark.parametrize(
    "existing_versions, new_version, expected_predecessor", PREV_TAG_CASES
)
def test_prev_tag_picks_semver_predecessor(
    existing_versions, new_version, expected_predecessor, run_bash, tmp_path
):
    repo = _local_repo_with_version_tags(tmp_path, PLUGIN, existing_versions)
    result = run_bash(
        [PREV_RELEASE_TAG_SCRIPT.as_posix(), PLUGIN, new_version], cwd=repo
    )
    assert result.returncode == 0, (
        f"expected rc 0, got {result.returncode}. "
        f"stdout: {result.stdout!r} stderr: {result.stderr!r}"
    )
    assert result.stdout.strip() == f"{PLUGIN}--v{expected_predecessor}"


def test_prev_tag_excludes_the_tag_being_created(run_bash, tmp_path):
    repo = _local_repo_with_version_tags(tmp_path, PLUGIN, ["0.0.1", "0.0.2"])
    result = run_bash([PREV_RELEASE_TAG_SCRIPT.as_posix(), PLUGIN, "0.0.2"], cwd=repo)
    assert result.returncode == 0, f"stderr: {result.stderr!r}"
    assert result.stdout.strip() == f"{PLUGIN}--v0.0.1"


def test_prev_tag_ignores_foreign_and_malformed_tags(run_bash, tmp_path):
    # The new version (20.0.0) is deliberately higher than every foreign/
    # malformed tag below (9.9.9, 5.0.0, leniently-parsed 01.0.0 == 1.0.0),
    # so each of them WOULD be a valid, higher-than-0.0.1 "strictly lower"
    # candidate if the namespace filter or leading-zero validation were
    # missing -- unlike the original 1.0.0 target, under which they were all
    # excluded by version-ordering alone and the filters were never actually
    # exercised (test-critic tautology::F2).
    repo = _local_repo_with_version_tags(tmp_path, PLUGIN, ["0.0.1"])
    _git("tag", f"src/{PLUGIN}--v9.9.9", cwd=repo)
    _git("tag", "other-plugin--v5.0.0", cwd=repo)
    _git("tag", f"{PLUGIN}--v01.0.0", cwd=repo)
    _git("tag", f"{PLUGIN}--vbad", cwd=repo)
    result = run_bash([PREV_RELEASE_TAG_SCRIPT.as_posix(), PLUGIN, "20.0.0"], cwd=repo)
    assert result.returncode == 0, f"stderr: {result.stderr!r}"
    assert result.stdout.strip() == f"{PLUGIN}--v0.0.1", (
        "expected src/*, a foreign plugin's tag, and malformed versions "
        "(leading zero, non-semver) to be ignored even though each is "
        f"individually 'strictly lower' than 20.0.0, got {result.stdout!r}"
    )


def test_prev_tag_first_release_prints_nothing(run_bash, tmp_path):
    repo = _local_repo_with_version_tags(tmp_path, PLUGIN, [])
    result = run_bash([PREV_RELEASE_TAG_SCRIPT.as_posix(), PLUGIN, "0.0.1"], cwd=repo)
    assert result.returncode == 0, f"stderr: {result.stderr!r}"
    assert result.stdout.strip() == ""


@pytest.mark.parametrize(
    "bad_version",
    ["1.0", "01.0.0", "1.0.0+build"],
    ids=["two-component", "leading-zero", "build-metadata"],
)
def test_prev_tag_rejects_invalid_semver(bad_version, run_bash, tmp_path):
    repo = _local_repo_with_version_tags(tmp_path, PLUGIN, [])
    result = run_bash([PREV_RELEASE_TAG_SCRIPT.as_posix(), PLUGIN, bad_version], cwd=repo)
    assert result.returncode == 2, (
        f"expected exit code 2 for invalid version {bad_version!r}, got "
        f"{result.returncode}. stdout: {result.stdout!r} stderr: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# R2 -- release-preflight.sh: fail-fast before any side effect.
# ---------------------------------------------------------------------------

def test_preflight_missing_prev_marker_prints_bootstrap(run_bash, tmp_path):
    origin, clone = _origin_and_clone(tmp_path, PLUGIN, ["0.0.7"], src_tag_versions=())
    remote_before = _ls_remote_tags(tmp_path, origin)

    result = run_bash(
        [RELEASE_PREFLIGHT_SCRIPT.as_posix(), PLUGIN, "0.0.8"],
        cwd=clone,
        env={"GITHUB_REF": "refs/heads/main"},
    )

    assert result.returncode == 1, (
        f"expected rc 1, got {result.returncode}. "
        f"stdout: {result.stdout!r} stderr: {result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    prev_tag = f"{PLUGIN}--v0.0.7"
    assert f"git tag src/{prev_tag} <head_sha of {prev_tag}'s release.yml run>" in combined, (
        f"expected the exact bootstrap `git tag` command in output, got: {combined!r}"
    )
    assert f"git push origin src/{prev_tag}" in combined, (
        f"expected the exact bootstrap `git push` command in output, got: {combined!r}"
    )
    remote_after = _ls_remote_tags(tmp_path, origin)
    assert remote_after == remote_before, (
        "release-preflight.sh must write nothing to origin before failing"
    )


def test_preflight_succeeds_when_prev_marker_present(run_bash, tmp_path):
    origin, clone = _origin_and_clone(
        tmp_path, PLUGIN, ["0.0.7"], src_tag_versions=["0.0.7"]
    )
    github_output = tmp_path / "github_output.txt"
    result = run_bash(
        [RELEASE_PREFLIGHT_SCRIPT.as_posix(), PLUGIN, "0.0.8"],
        cwd=clone,
        env={"GITHUB_REF": "refs/heads/main", "GITHUB_OUTPUT": str(github_output)},
    )
    assert result.returncode == 0, (
        f"stdout: {result.stdout!r} stderr: {result.stderr!r}"
    )
    # The value written for main_sha must actually be the clone's real HEAD
    # -- assemble places the src/<TAG> marker there, so a script that writes
    # `main_sha=` with an empty or wrong value must fail this (test-critic
    # tautology::F3: the original assertion only checked the key's presence).
    expected_sha = _git("rev-parse", "HEAD", cwd=clone).stdout.strip()
    output_text = github_output.read_text(encoding="utf-8") if github_output.exists() else ""
    assert f"prev_tag={PLUGIN}--v0.0.7" in output_text, output_text
    assert f"main_sha={expected_sha}" in output_text, (
        f"expected main_sha to equal the clone's real HEAD {expected_sha!r}, "
        f"got: {output_text!r}"
    )


def test_preflight_first_release_has_empty_prev_tag(run_bash, tmp_path):
    origin, clone = _origin_and_clone(tmp_path, PLUGIN, [], src_tag_versions=())
    github_output = tmp_path / "github_output.txt"
    result = run_bash(
        [RELEASE_PREFLIGHT_SCRIPT.as_posix(), PLUGIN, "0.0.1"],
        cwd=clone,
        env={"GITHUB_REF": "refs/heads/main", "GITHUB_OUTPUT": str(github_output)},
    )
    assert result.returncode == 0, (
        f"stdout: {result.stdout!r} stderr: {result.stderr!r}"
    )
    output_text = github_output.read_text(encoding="utf-8") if github_output.exists() else ""
    assert "prev_tag=\n" in output_text or output_text.rstrip("\n").endswith("prev_tag="), (
        f"expected an empty prev_tag= output for a first release, got {output_text!r}"
    )


def test_preflight_rejects_existing_src_marker_for_new_tag(run_bash, tmp_path):
    # src/<PREV_TAG> (0.0.7) is ALSO seeded here, alongside the offending
    # src/<TAG> (0.0.8) -- without it, step 5 (missing predecessor marker)
    # would independently fail this run regardless of whether step 3's
    # existing-src/<TAG> check does anything at all, so rc==1 would never
    # isolate that check (test-critic tautology::F1).
    origin, clone = _origin_and_clone(
        tmp_path, PLUGIN, ["0.0.7"], src_tag_versions=["0.0.7", "0.0.8"]
    )
    remote_before = _ls_remote_tags(tmp_path, origin)
    result = run_bash(
        [RELEASE_PREFLIGHT_SCRIPT.as_posix(), PLUGIN, "0.0.8"],
        cwd=clone,
        env={"GITHUB_REF": "refs/heads/main"},
    )
    assert result.returncode == 1, (
        f"stdout: {result.stdout!r} stderr: {result.stderr!r}"
    )
    # The failure must be attributable to the existing src/<TAG> specifically
    # (not e.g. a missing predecessor marker, which is now seeded and would
    # otherwise pass) -- the plan's exact phrasing for this case is "use a
    # new version" (test-critic tautology::F5: the prior membership recheck
    # below was implied by remote_after == remote_before and asserted
    # nothing new).
    combined = result.stdout + result.stderr
    assert "use a new version" in combined.lower(), (
        f"expected an error naming the existing src/<TAG> and telling the "
        f"operator to use a new version, got: {combined!r}"
    )
    remote_after = _ls_remote_tags(tmp_path, origin)
    assert remote_after == remote_before, "an existing src/<TAG> must never be deleted or moved"


def test_preflight_rejects_existing_release_tag(run_bash, tmp_path):
    origin, clone = _origin_and_clone(
        tmp_path, PLUGIN, ["0.0.7", "0.0.8"], src_tag_versions=["0.0.7"]
    )
    result = run_bash(
        [RELEASE_PREFLIGHT_SCRIPT.as_posix(), PLUGIN, "0.0.8"],
        cwd=clone,
        env={"GITHUB_REF": "refs/heads/main"},
    )
    assert result.returncode == 1, (
        f"stdout: {result.stdout!r} stderr: {result.stderr!r}"
    )


def test_preflight_rejects_non_main_ref(run_bash, tmp_path):
    origin, clone = _origin_and_clone(
        tmp_path, PLUGIN, ["0.0.7"], src_tag_versions=["0.0.7"]
    )
    result = run_bash(
        [RELEASE_PREFLIGHT_SCRIPT.as_posix(), PLUGIN, "0.0.8"],
        cwd=clone,
        env={"GITHUB_REF": "refs/heads/some-other-branch"},
    )
    assert result.returncode == 1, (
        f"stdout: {result.stdout!r} stderr: {result.stderr!r}"
    )


def test_preflight_rejects_invalid_version(run_bash, tmp_path):
    origin, clone = _origin_and_clone(tmp_path, PLUGIN, [], src_tag_versions=())
    result = run_bash(
        [RELEASE_PREFLIGHT_SCRIPT.as_posix(), PLUGIN, "not-a-version"],
        cwd=clone,
        env={"GITHUB_REF": "refs/heads/main"},
    )
    assert result.returncode == 2, (
        f"stdout: {result.stdout!r} stderr: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# R2b -- a tagless checkout is never mistaken for "first release".
# ---------------------------------------------------------------------------

def test_preflight_fails_when_local_tags_missing(run_bash, tmp_path):
    origin, clone = _origin_and_clone(
        tmp_path, PLUGIN, ["0.0.7"], src_tag_versions=["0.0.7"]
    )
    # Simulate a checkout that fetched history but not this tag (e.g. no
    # `fetch-depth: 0`) -- the tag is on `origin` but absent from the local
    # clone the script actually runs against.
    _git("tag", "-d", f"{PLUGIN}--v0.0.7", cwd=clone)

    result = run_bash(
        [RELEASE_PREFLIGHT_SCRIPT.as_posix(), PLUGIN, "0.0.8"],
        cwd=clone,
        env={"GITHUB_REF": "refs/heads/main"},
    )
    assert result.returncode == 1, (
        f"expected rc 1 (not rc 0 with an empty prev_tag, which would "
        f"misread this as a first release), got {result.returncode}. "
        f"stdout: {result.stdout!r} stderr: {result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert f"{PLUGIN}--v0.0.7" in combined, (
        f"expected the missing-locally tag to be named in the error, got {combined!r}"
    )
    assert "fetch-depth" in combined, (
        f"expected a fetch-depth hint in the error, got {combined!r}"
    )


def test_preflight_fails_fatally_when_origin_unreachable(run_bash, tmp_path):
    repo = tmp_path / "lonely-clone"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _configure_identity(repo)
    _git("commit", "--allow-empty", "-m", "init", cwd=repo)
    _git("remote", "add", "origin", str(tmp_path / "does-not-exist.git"), cwd=repo)

    result = run_bash(
        [RELEASE_PREFLIGHT_SCRIPT.as_posix(), PLUGIN, "0.0.1"],
        cwd=repo,
        env={"GITHUB_REF": "refs/heads/main"},
    )
    assert result.returncode != 0, (
        "an `origin` that cannot be reached at all must be a fatal error, "
        f"not a silent empty-tag-list pass. stdout: {result.stdout!r} "
        f"stderr: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# R3 / R4 -- marketplace-payload.sh: byte-for-byte changelog round-trip,
# omission when empty, UTF-8 byte-budget truncation, no ASCII-escaping.
# ---------------------------------------------------------------------------

FROZEN_KEYS_WITHOUT_CHANGELOG = {
    "name", "description", "repo", "category", "version", "ref", "icon", "description_url",
}


def _run_payload_script(
    run_bash, tmp_path, *, env_overrides=None, changelog_present=True, changelog_body=""
):
    env = dict(DEFAULT_PAYLOAD_ENV)
    if env_overrides:
        env.update(env_overrides)

    payload_file = tmp_path / "payload.json"
    changelog_file = tmp_path / "changelog.md"
    if changelog_present:
        changelog_file.write_text(changelog_body, encoding="utf-8", newline="")

    env["PAYLOAD_FILE"] = str(payload_file)
    env["CHANGELOG_FILE"] = str(changelog_file)

    result = run_bash([MARKETPLACE_PAYLOAD_SCRIPT.as_posix()], cwd=tmp_path, env=env)
    return result, payload_file, env


def test_payload_hostile_changelog_round_trips(run_bash, tmp_path):
    result, payload_file, _ = _run_payload_script(
        run_bash, tmp_path, changelog_present=True, changelog_body=HOSTILE_CHANGELOG + "\n"
    )
    assert result.returncode == 0, (
        f"expected rc 0, got {result.returncode}. stderr: {result.stderr!r}"
    )
    body = json.loads(payload_file.read_text(encoding="utf-8"))
    assert set(body) == {"event_type", "client_payload"}
    assert body["event_type"] == "plugin-release"
    client_payload = body["client_payload"]
    assert set(client_payload) == FROZEN_KEYS_WITHOUT_CHANGELOG | {"changelog"}
    assert client_payload["changelog"] == HOSTILE_CHANGELOG, (
        "the trailing one newline gh's --jq output appends must be stripped, "
        "and nothing else touched"
    )


def test_payload_envelope_shape(run_bash, tmp_path):
    result, payload_file, _ = _run_payload_script(run_bash, tmp_path, changelog_present=False)
    assert result.returncode == 0, f"stderr: {result.stderr!r}"
    body = json.loads(payload_file.read_text(encoding="utf-8"))
    assert set(body) == {"event_type", "client_payload"}
    assert body["event_type"] == "plugin-release"


def test_payload_frozen_values_with_custom_repo(run_bash, tmp_path):
    result, payload_file, env = _run_payload_script(
        run_bash, tmp_path,
        env_overrides={"REPO": "SomeOrg/some-other-repo"},
        changelog_present=False,
    )
    assert result.returncode == 0, f"stderr: {result.stderr!r}"
    body = json.loads(payload_file.read_text(encoding="utf-8"))
    client_payload = body["client_payload"]
    assert client_payload["repo"] == "SomeOrg/some-other-repo"
    assert client_payload["version"] == env["VERSION"]
    assert client_payload["ref"] == env["TAG"]
    assert client_payload["icon"] == (
        f"https://raw.githubusercontent.com/SomeOrg/some-other-repo/{env['TAG']}/assets/icon.png"
    )
    assert client_payload["description_url"] == (
        f"https://raw.githubusercontent.com/SomeOrg/some-other-repo/{env['TAG']}/description.md"
    )


def test_payload_survives_hostile_name_and_description(run_bash, tmp_path):
    overrides = {
        "NAME": 'weird "name" with \\backslash\\ and `backtick`',
        "DESC": 'desc with\nnewline and "quotes"',
    }
    result, payload_file, _ = _run_payload_script(
        run_bash, tmp_path, env_overrides=overrides, changelog_present=False
    )
    assert result.returncode == 0, f"stderr: {result.stderr!r}"
    body = json.loads(payload_file.read_text(encoding="utf-8"))
    client_payload = body["client_payload"]
    assert client_payload["name"] == overrides["NAME"]
    assert client_payload["description"] == overrides["DESC"]


@pytest.mark.parametrize(
    "changelog_present, changelog_body",
    [
        pytest.param(False, "", id="file-absent"),
        pytest.param(True, "", id="empty-file"),
        pytest.param(True, "   \n\t  \n", id="whitespace-only"),
    ],
)
def test_payload_omits_changelog_when_empty(changelog_present, changelog_body, run_bash, tmp_path):
    result, payload_file, env = _run_payload_script(
        run_bash, tmp_path, changelog_present=changelog_present, changelog_body=changelog_body
    )
    assert result.returncode == 0, f"stderr: {result.stderr!r}"
    body = json.loads(payload_file.read_text(encoding="utf-8"))
    client_payload = body["client_payload"]
    assert "changelog" not in client_payload
    assert set(client_payload) == FROZEN_KEYS_WITHOUT_CHANGELOG
    assert client_payload["ref"] == env["TAG"]


@pytest.mark.parametrize(
    "changelog_present, changelog_body, expect_warning",
    [
        pytest.param(False, "", True, id="file-absent"),
        pytest.param(True, "", True, id="empty-file"),
        pytest.param(True, "   \n\t  \n", True, id="whitespace-only"),
        pytest.param(True, "Real content", False, id="non-empty"),
    ],
)
def test_payload_warning_iff_omitted(
    changelog_present, changelog_body, expect_warning, run_bash, tmp_path
):
    result, payload_file, _ = _run_payload_script(
        run_bash, tmp_path, changelog_present=changelog_present, changelog_body=changelog_body
    )
    assert result.returncode == 0, f"stderr: {result.stderr!r}"
    combined = result.stdout + result.stderr
    warning_present = "::warning::" in combined
    assert warning_present == expect_warning, (
        f"warning_present={warning_present}, expected {expect_warning}. "
        f"combined output: {combined!r}"
    )
    if expect_warning:
        assert "dispatching without a changelog" in combined


def test_oversized_changelog_is_truncated_with_link(run_bash, tmp_path):
    oversized = "a" * 80000
    result, payload_file, env = _run_payload_script(
        run_bash, tmp_path, changelog_present=True, changelog_body=oversized
    )
    assert result.returncode == 0, f"stderr: {result.stderr!r}"
    body = json.loads(payload_file.read_text(encoding="utf-8"))
    changelog = body["client_payload"]["changelog"]
    changelog_bytes = len(changelog.encode("utf-8"))
    assert changelog_bytes < len(oversized.encode("utf-8"))
    assert changelog_bytes <= TRUNCATE_LIMIT + 300
    link = f"https://github.com/{env['REPO']}/releases/tag/{env['TAG']}"
    assert link in changelog
    marker_index = changelog.find("\n\n_")
    assert marker_index != -1
    survived = changelog[:marker_index]
    survived_bytes = len(survived.encode("utf-8"))
    assert survived_bytes >= TRUNCATE_LIMIT - 10
    assert oversized.startswith(survived)


def test_truncation_budget_counts_utf8_bytes_not_characters(run_bash, tmp_path):
    # 20000 chars but 60000 bytes (euro sign is 3 bytes in UTF-8) -- a
    # character-based budget would never fire (20000 <= 30000).
    oversized = "\u20ac" * 20000
    result, payload_file, _ = _run_payload_script(
        run_bash, tmp_path, changelog_present=True, changelog_body=oversized
    )
    assert result.returncode == 0, f"stderr: {result.stderr!r}"
    body = json.loads(payload_file.read_text(encoding="utf-8"))
    changelog = body["client_payload"]["changelog"]
    changelog_bytes = len(changelog.encode("utf-8"))
    assert changelog_bytes < len(oversized.encode("utf-8"))
    assert changelog_bytes <= TRUNCATE_LIMIT + 300


def test_truncation_never_emits_invalid_utf8(run_bash, tmp_path):
    # 1 ASCII byte + 20000 euro signs (3 bytes each): a byte-offset cut at
    # exactly TRUNCATE_LIMIT lands mid-codepoint unless truncation decodes
    # with errors="ignore".
    oversized = "x" + "\u20ac" * 20000
    result, payload_file, _ = _run_payload_script(
        run_bash, tmp_path, changelog_present=True, changelog_body=oversized
    )
    assert result.returncode == 0, f"stderr: {result.stderr!r}"
    body = json.loads(payload_file.read_text(encoding="utf-8"))
    changelog = body["client_payload"]["changelog"]
    assert "\ufffd" not in changelog
    changelog.encode("utf-8")  # must still be valid, re-encodable UTF-8


def test_changelog_just_under_limit_is_untouched(run_bash, tmp_path):
    exact = "a" * TRUNCATE_LIMIT
    result, payload_file, _ = _run_payload_script(
        run_bash, tmp_path, changelog_present=True, changelog_body=exact
    )
    assert result.returncode == 0, f"stderr: {result.stderr!r}"
    body = json.loads(payload_file.read_text(encoding="utf-8"))
    assert body["client_payload"]["changelog"] == exact


def test_payload_is_raw_utf8_not_ascii_escaped(run_bash, tmp_path):
    changelog_body = "caf\u00e9 \u4e2d\u6587 \U0001F600"  # café, CJK, emoji
    result, payload_file, _ = _run_payload_script(
        run_bash, tmp_path, changelog_present=True, changelog_body=changelog_body
    )
    assert result.returncode == 0, f"stderr: {result.stderr!r}"
    raw_bytes = payload_file.read_bytes()
    assert changelog_body.encode("utf-8") in raw_bytes
    assert b"\\u00e9" not in raw_bytes and b"\\u4e2d" not in raw_bytes
    body = json.loads(raw_bytes.decode("utf-8"))
    assert body["client_payload"]["changelog"] == changelog_body


# ---------------------------------------------------------------------------
# R3b -- the real fetch-step `run:` text (as it exists in the workflow file
# today) piped through the real marketplace-payload.sh preserves a body's
# trailing blank lines byte for byte (Amendment §5 regression). A `gh` stub
# on PATH mimics `gh release view --json body -q '.body // empty'`'s raw
# output shape: `cat "$BODY_FILE"; printf '\n'` -- the body's own bytes,
# followed by exactly the one trailing newline gh's own `--jq` rendering
# appends, which marketplace-payload.sh must then strip exactly once.
#
# The fetch step's *wiring* (swapping `2>/dev/null || true` for a visible
# `::warning::` on failure) is `release.yml`/`dispatch.yml` production-code
# work for phase=implement, done together with the workflow edits (plan
# dependency step 2) -- not here. Every assertion below that depends on that
# still-pending wiring change is deliberately ordered AFTER the
# marketplace-payload.sh success assertion, so this test's RED reason stays
# "script missing (rc 127)" regardless of the fetch step's current text.
# ---------------------------------------------------------------------------

def _load_workflow(path):
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _fetch_step_run_text(path):
    workflow = _load_workflow(path)
    job = FETCH_STEP_JOB[path]
    for step in workflow["jobs"][job]["steps"]:
        if step.get("name") == FETCH_STEP_NAME:
            return step.get("run", "")
    raise AssertionError(f"step {FETCH_STEP_NAME!r} not found in job {job!r} of {path}")


def _make_gh_stub(tmp_path, body_bytes, exit_code=0):
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir(exist_ok=True)
    body_file = tmp_path / "gh-stub-body.txt"
    body_file.write_bytes(body_bytes)

    gh_stub = stub_dir / "gh"
    gh_stub.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'if [ "${1:-}" = "release" ] && [ "${2:-}" = "view" ]; then\n'
        f'  if [ "{exit_code}" != "0" ]; then\n'
        '    echo "gh: stub failure" >&2\n'
        f'    exit {exit_code}\n'
        "  fi\n"
        f'  cat "{body_file.as_posix()}"\n'
        "  printf '\\n'\n"
        "  exit 0\n"
        "fi\n"
        'echo "unexpected gh invocation: $*" >&2\n'
        "exit 99\n",
        encoding="utf-8",
        newline="\n",
    )
    gh_stub.chmod(0o755)
    return stub_dir


@pytest.mark.parametrize("workflow", [RELEASE_YML, DISPATCH_YML], ids=["release.yml", "dispatch.yml"])
def test_fetch_to_payload_preserves_trailing_newlines(workflow, run_bash, tmp_path):
    body_bytes = "line one\n\nline two\n\n".encode("utf-8")
    stub_dir = _make_gh_stub(tmp_path, body_bytes, exit_code=0)

    changelog_file = tmp_path / "changelog.md"
    tag = f"{PLUGIN}--v0.0.2"
    repo = f"seretos-agents/{PLUGIN}"

    fetch_env = {
        "PATH": f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "GH_TOKEN": "dummy-token",
        "TAG": tag,
        "GITHUB_REPOSITORY": repo,
        "CHANGELOG_FILE": str(changelog_file),
    }
    fetch_result = run_bash(["-c", _fetch_step_run_text(workflow)], cwd=tmp_path, env=fetch_env)
    assert fetch_result.returncode == 0, (
        f"the fetch step itself must never fail the job. "
        f"stdout: {fetch_result.stdout!r} stderr: {fetch_result.stderr!r}"
    )

    payload_file = tmp_path / "payload.json"
    payload_env = {
        "NAME": PLUGIN,
        "DESC": "A sample plugin.",
        "VERSION": "0.0.2",
        "TAG": tag,
        "REPO": repo,
        "CHANGELOG_FILE": str(changelog_file),
        "PAYLOAD_FILE": str(payload_file),
    }
    payload_result = run_bash(
        [MARKETPLACE_PAYLOAD_SCRIPT.as_posix()], cwd=tmp_path, env=payload_env
    )
    assert payload_result.returncode == 0, (
        f"marketplace-payload.sh must exist and succeed. "
        f"stdout: {payload_result.stdout!r} stderr: {payload_result.stderr!r}"
    )

    body = json.loads(payload_file.read_text(encoding="utf-8"))
    assert body["client_payload"]["changelog"] == "line one\n\nline two\n\n"

    raw = payload_file.read_bytes()
    assert b"\r" not in raw
    assert raw.count(b"\n") == 1
    assert raw.endswith(b"\n")


@pytest.mark.parametrize("workflow", [RELEASE_YML, DISPATCH_YML], ids=["release.yml", "dispatch.yml"])
def test_fetch_to_payload_omits_key_when_gh_fails(workflow, run_bash, tmp_path):
    stub_dir = _make_gh_stub(tmp_path, b"unused", exit_code=1)
    changelog_file = tmp_path / "changelog.md"
    tag = f"{PLUGIN}--v0.0.2"
    repo = f"seretos-agents/{PLUGIN}"

    fetch_env = {
        "PATH": f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "GH_TOKEN": "dummy-token",
        "TAG": tag,
        "GITHUB_REPOSITORY": repo,
        "CHANGELOG_FILE": str(changelog_file),
    }
    fetch_result = run_bash(["-c", _fetch_step_run_text(workflow)], cwd=tmp_path, env=fetch_env)
    assert fetch_result.returncode == 0, "a failing `gh` must not fail the fetch step"
    # The fetch step's new runtime behaviour (swapping the old silent
    # `2>/dev/null || true` for a visible warning) must actually fire here --
    # asserting only rc 0 and key omission was also satisfied by the OLD
    # silent-swallow fetch step, so it never exercised this change
    # (test-critic tautology::F4).
    fetch_combined = fetch_result.stdout + fetch_result.stderr
    assert "::warning::gh release view failed" in fetch_combined, (
        f"expected the fetch step to print a visible ::warning:: on a "
        f"failing `gh`, got: {fetch_combined!r}"
    )

    payload_file = tmp_path / "payload.json"
    payload_env = {
        "NAME": PLUGIN,
        "DESC": "A sample plugin.",
        "VERSION": "0.0.2",
        "TAG": tag,
        "REPO": repo,
        "CHANGELOG_FILE": str(changelog_file),
        "PAYLOAD_FILE": str(payload_file),
    }
    payload_result = run_bash(
        [MARKETPLACE_PAYLOAD_SCRIPT.as_posix()], cwd=tmp_path, env=payload_env
    )
    assert payload_result.returncode == 0, (
        f"marketplace-payload.sh must exist and succeed. "
        f"stderr: {payload_result.stderr!r}"
    )

    body = json.loads(payload_file.read_text(encoding="utf-8"))
    assert "changelog" not in body["client_payload"]
    assert len(body["client_payload"]) == 8


@pytest.mark.parametrize("workflow", [RELEASE_YML, DISPATCH_YML], ids=["release.yml", "dispatch.yml"])
def test_fetch_to_payload_omits_key_for_blank_body(workflow, run_bash, tmp_path):
    stub_dir = _make_gh_stub(tmp_path, b"\n\n\n", exit_code=0)
    changelog_file = tmp_path / "changelog.md"
    tag = f"{PLUGIN}--v0.0.2"
    repo = f"seretos-agents/{PLUGIN}"

    fetch_env = {
        "PATH": f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "GH_TOKEN": "dummy-token",
        "TAG": tag,
        "GITHUB_REPOSITORY": repo,
        "CHANGELOG_FILE": str(changelog_file),
    }
    fetch_result = run_bash(["-c", _fetch_step_run_text(workflow)], cwd=tmp_path, env=fetch_env)
    assert fetch_result.returncode == 0

    payload_file = tmp_path / "payload.json"
    payload_env = {
        "NAME": PLUGIN,
        "DESC": "A sample plugin.",
        "VERSION": "0.0.2",
        "TAG": tag,
        "REPO": repo,
        "CHANGELOG_FILE": str(changelog_file),
        "PAYLOAD_FILE": str(payload_file),
    }
    payload_result = run_bash(
        [MARKETPLACE_PAYLOAD_SCRIPT.as_posix()], cwd=tmp_path, env=payload_env
    )
    assert payload_result.returncode == 0, (
        f"marketplace-payload.sh must exist and succeed. "
        f"stderr: {payload_result.stderr!r}"
    )

    body = json.loads(payload_file.read_text(encoding="utf-8"))
    assert "changelog" not in body["client_payload"]
