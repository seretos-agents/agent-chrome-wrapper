"""Shared pytest fixtures for the chrome_wrapper_plugin test suite."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

import pytest

# All env-vars resolve_session_id() probes, in probe order.
_SESSION_ENV_VARS = (
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_SESSION_ID",
    "ANTHROPIC_SESSION_ID",
    "MCP_SESSION_ID",
)


@pytest.fixture(autouse=True)
def _isolate_session_env(monkeypatch):
    """Clear every session-id env-var before each test.

    Without this, the suite's outcome would depend on the ambient
    environment it happens to run under (e.g. a real Claude Code session
    exporting CLAUDE_CODE_SESSION_ID) rather than on the code under test.
    """
    for var in _SESSION_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# `run_bash` -- ticket #44: drives the real `.github/scripts/*.sh` scripts
# (and, for tests/test_release_scripts.py's R3b, a workflow step's raw `run:`
# text) via a real bash subprocess against real temp git repos.
#
# On win32, a bare `bash` resolved from PATH is the WSL stub (it either fails
# outright or drops into a different filesystem/PATH universe than the one
# this test run and its fixtures live in) -- Git-for-Windows' bash.exe must be
# used by absolute path instead. There is deliberately NO skip branch here:
# per the plan (simplifier::M1), a missing bash or `jq` FAILS the suite on
# every platform rather than silently skipping the scripts' coverage -- `jq`
# is a hard runtime dependency of marketplace-payload.sh, and windows-latest
# (the only OS release.yml's `assemble`/`stamp` jobs run on) ships it.
# ---------------------------------------------------------------------------

_GIT_BASH_PATH = r"C:\Program Files\Git\bin\bash.exe"


def _resolve_bash_path():
    if platform.system() == "Windows":
        candidate = Path(_GIT_BASH_PATH)
        if not candidate.exists():
            pytest.fail(
                f"Git-for-Windows bash.exe not found at {candidate} -- "
                "required to run tests/test_release_scripts.py against the "
                "real .github/scripts/*.sh scripts. A bare `bash` on PATH "
                "resolves to the WSL stub on win32 and must not be used. "
                "Install Git for Windows, or adjust this path if it moved."
            )
        return str(candidate)
    found = shutil.which("bash")
    if not found:
        pytest.fail(
            "bash not found on PATH -- required to run the release scripts "
            "under test (tests/test_release_scripts.py)."
        )
    return found


@pytest.fixture(scope="session")
def bash_path():
    path = _resolve_bash_path()
    probe = subprocess.run(
        [path, "-c", "command -v jq"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        pytest.fail(
            f"jq not found on the PATH seen by {path!r} -- jq is a hard "
            "runtime dependency of .github/scripts/marketplace-payload.sh. "
            f"stdout={probe.stdout!r} stderr={probe.stderr!r}"
        )
    return path


@pytest.fixture
def run_bash(bash_path):
    """Run `bash_path <*bash_args>`, returning the completed subprocess.

    `env`, when given, is applied as overrides on top of a copy of the
    current process's environment (so PATH, HOME, etc. are inherited unless
    explicitly overridden) -- not a full replacement, so callers only need
    to name what they're adding/changing (e.g. prepending a stub-`gh`
    directory to PATH, or setting GITHUB_REF/GITHUB_OUTPUT/CHANGELOG_FILE).
    """

    def _run(bash_args, *, cwd=None, env=None, input=None):
        run_env = os.environ.copy()
        if env:
            run_env.update(env)
        return subprocess.run(
            [bash_path, *bash_args],
            cwd=os.fspath(cwd) if cwd is not None else None,
            env=run_env,
            capture_output=True,
            text=True,
            input=input,
        )

    return _run
