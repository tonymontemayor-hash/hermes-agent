"""Custom-branch updater contract (XHIGH — preserve a tracked custom branch).

Tony's real incident: an install intentionally running a custom branch
(``sofia-v2026.9.14-custom``, tracked to the fork) ran the Desktop updater and
it re-targeted to ``main``, synced upstream, left HEAD detached and suggested
``checkout main``. The updater must instead STAY on the attached tracked custom
branch, fast-forward ONLY from its real tracking ref, and never silently switch
to main or leave the checkout detached.

Contract under test (Caso A/B/C/D/E + FF-only):

* A — attached tracked custom branch + remote advanced -> update STAYS on the
  branch, FF onto the real tracking ref (``fork/<branch>``), tracking preserved,
  never checks out main, never detached, upstream/main NOT consumed.
* B — standard main checkout -> existing behavior preserved (the legitimate
  ``main`` default).
* C — detached HEAD + no branch -> safe failure, NO silent ``--branch main``.
* D — attached non-main branch without a tracking upstream + no branch ->
  safe failure (STRICT: no remote guess, no main fallback).
* E — dirty tree -> changes are never silently discarded.
* FF-only — a tracked custom branch that DIVERGED fails safe (no merge commit,
  no reset, no rebase, local commits untouched).

All tests run against REAL git repositories (init, commit, branch, push to a
bare "remote", tracking) with the real git plumbing — the long tail of the
update (dependency install, gateway restart) is stopped with a sentinel, exactly
like ``test_update_parked_branch_guard.py``.
"""

import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import main as hermes_main
import hermes_cli.main_web_build as main_web_build
import hermes_cli.main_install_repair as main_install_repair
from hermes_cli import update_cmd


GIT = ["git"]
CUSTOM = "sofia-v2026.9.14-custom"


def _git(cwd, *args, check=True):
    return subprocess.run(
        GIT + list(args),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


def _commit(repo, path, text, msg):
    (repo / path).write_text(text)
    _git(repo, "add", path)
    _git(repo, "commit", "-qm", msg)


def _mk_repo(tmp_path, name):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _commit(repo, "a.txt", "one\n", "c1")
    return repo


# ---------------------------------------------------------------------------
# Fixtures (real git)
# ---------------------------------------------------------------------------

@pytest.fixture()
def tracked_custom(tmp_path):
    """Caso A topology (Tony's real shape): a bare *fork* remote + a local
    checkout attached to a custom branch that TRACKS the fork's custom ref.
    The fork's custom branch is one commit ahead of the checkout.

    Returns a dict with paths and a ``main_only_commit`` sha that exists ONLY on
    origin/main (to prove the custom update never consumes upstream/main).
    """
    fork = tmp_path / "fork.git"
    _git(tmp_path, "init", "-q", "--bare", str(fork))
    # Seed the fork from a throwaway source so it has main + the custom branch.
    seed = _mk_repo(tmp_path, "seed")
    _git(seed, "remote", "add", "fork", str(fork))
    _git(seed, "push", "-q", "fork", "main")
    # Fork's custom branch: branched from main, then the working copy tracks it.
    _git(seed, "checkout", "-qb", CUSTOM)
    _commit(seed, "c.txt", "custom-base\n", "custom-base")
    _git(seed, "push", "-q", "-u", "fork", CUSTOM)

    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", "-b", CUSTOM, str(fork), str(work))
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    # Tony's real shape: the custom branch tracks a remote NAMED `fork`, not
    # `origin`. Renaming proves the updater uses the branch's REAL tracking ref
    # (``fork/<branch>``) — a hardcoded ``origin/<branch>`` fetch would fail here.
    _git(work, "remote", "rename", "origin", "fork")

    # main-only commit: lives ONLY on fork/main, never on the custom branch.
    _git(seed, "checkout", "-q", "main")
    _commit(seed, "m.txt", "main-only\n", "main-only")
    _git(seed, "push", "-q", "fork", "main")
    main_only_sha = _git(seed, "rev-parse", "HEAD").stdout.strip()

    # Now advance the fork's CUSTOM branch by one commit (the update to pull).
    # Must be on the CUSTOM branch — committing on main would land it there.
    _git(seed, "checkout", "-q", CUSTOM)
    _commit(seed, "c.txt", "custom-advance\n", "custom-advance")
    _git(seed, "push", "-q", "fork", CUSTOM)
    custom_tip = _git(fork, "rev-parse", f"refs/heads/{CUSTOM}").stdout.strip()

    # Make sure the working copy does NOT yet have the advanced commit locally.
    assert _git(work, "rev-parse", "HEAD").stdout.strip() != custom_tip

    return {
        "fork": fork,
        "work": work,
        "main_only_sha": main_only_sha,
        "custom_tip": custom_tip,
    }


@pytest.fixture()
def main_checkout(tmp_path):
    """Caso B topology: a normal clone attached to main tracking origin/main,
    with origin one commit ahead."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    seed = _mk_repo(tmp_path, "seed")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")

    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", "-b", "main", str(origin), str(work))
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")

    _commit(seed, "m.txt", "main-advance\n", "main-advance")
    _git(seed, "push", "-q", "origin", "main")
    return {"origin": origin, "work": work}


@pytest.fixture()
def detached_head(tmp_path):
    """Caso C topology: a clone with a detached HEAD (no attached branch)."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    seed = _mk_repo(tmp_path, "seed")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", "-b", "main", str(origin), str(work))
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    _git(work, "checkout", "-q", "--detach")
    return work


@pytest.fixture()
def custom_no_upstream(tmp_path):
    """Caso D topology: attached to a NON-main branch that has NO tracking
    upstream (created locally, never pushed)."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    seed = _mk_repo(tmp_path, "seed")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", "-b", "main", str(origin), str(work))
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    _git(work, "checkout", "-qb", "local-only-feature")
    return work


class _StopFlow(Exception):
    """Sentinel: stop the update flow right after the git pull/branch logic
    (before the dependency install)."""


def _stop_flow(monkeypatch):
    """Patch the guard the dep sync resolves through ``_m()`` — the
    ``hermes_cli.main`` module — so the sentinel patches MAIN'S binding
    (AGENTS.md: patch where production reads; a patch on the
    ``update_cmd_deps`` definition site would pass silently), exactly as
    ``test_update_parked_branch_guard.py`` does."""
    monkeypatch.setattr(
        hermes_main,
        "_abort_dependency_sync_if_self_locked",
        lambda *a, **k: (_ for _ in ()).throw(_StopFlow()),
    )


def _patch_flow(monkeypatch, repo, *, fork_remote=False, origin_url=None):
    """Point ``_cmd_update_impl`` at a real repo and neuter the long tail — the
    monkeypatch surface of ``test_update_parked_branch_guard.py``. ``fork_remote``
    marks the checkout as a fork (Tony's shape) so the is_fork probe matches; the
    default is a non-fork origin-is-official checkout."""
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", repo)
    monkeypatch.setattr(hermes_main, "_is_windows", lambda: False)
    monkeypatch.setattr(main_install_repair, "_is_windows", lambda: False)
    monkeypatch.setattr(
        hermes_main, "_get_origin_url",
        lambda *a, **k: origin_url or "https://example.invalid/repo.git",
    )
    monkeypatch.setattr(update_cmd, "_is_fork", lambda *a, **k: fork_remote)
    monkeypatch.setattr(update_cmd, "_discard_lockfile_churn", lambda *a, **k: None)
    monkeypatch.setattr(update_cmd, "_normalize_managed_eol", lambda *a, **k: None)
    monkeypatch.setattr(hermes_main, "_clear_bytecode_cache", lambda *a, **k: 0)
    monkeypatch.setattr(hermes_main, "_record_bytecode_fingerprint", lambda *a, **k: None)
    monkeypatch.setattr(main_web_build, "_record_bytecode_fingerprint", lambda *a, **k: None)
    monkeypatch.setattr(hermes_main, "_run_pre_update_backup", lambda *a, **k: None)
    monkeypatch.setattr(hermes_main, "_pause_windows_gateways_for_update", lambda: None)
    monkeypatch.setattr(
        hermes_main, "_resume_windows_gateways_after_update", lambda *a, **k: None
    )
    monkeypatch.setattr(hermes_main, "_capture_active_lazy_features", lambda: [])
    monkeypatch.setattr(hermes_main, "_capture_active_tool_dependencies", lambda: [])


def _cur_branch(repo):
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _tracking(repo):
    r = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}",
             check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


@pytest.fixture(autouse=True)
def _no_config(monkeypatch):
    """Isolate from the machine's real config.yaml (default update strategies)."""
    import hermes_cli.config as hermes_config

    monkeypatch.setattr(hermes_config, "load_config", lambda: {})


# ---------------------------------------------------------------------------
# _infer_update_branch — pure unit (no argv synthesis of `main`)
# ---------------------------------------------------------------------------

def test_infer_detached_without_flag_returns_none(detached_head, monkeypatch):
    """Caso C: detached HEAD + no --branch -> None (the caller fails safe; the
    updater must NOT hand `hermes update --branch main`)."""
    # _infer_update_branch reads git via _git_run, whose default cwd is the
    # checkout (hermes_main.PROJECT_ROOT) — point it at the fixture.
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", detached_head)
    args = SimpleNamespace(branch=None)
    assert update_cmd._infer_update_branch(GIT, args) is None


def test_infer_detached_with_explicit_flag_returns_that_branch(detached_head, monkeypatch):
    """Caso C variant: detached + EXPLICIT --branch -> that branch (source
    origin/<branch>), never a synthesis to main."""
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", detached_head)
    args = SimpleNamespace(branch="main")
    r = update_cmd._infer_update_branch(GIT, args)
    assert r is not None
    assert (r.branch, r.source_ref) == ("main", "origin/main")


def test_infer_custom_no_upstream_returns_none(custom_no_upstream, monkeypatch):
    """Caso D STRICT: attached to a non-main branch without a tracking upstream
    + no --branch -> None (no remote guess, no main fallback)."""
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", custom_no_upstream)
    args = SimpleNamespace(branch=None)
    assert update_cmd._infer_update_branch(GIT, args) is None


def test_infer_main_attached_returns_main(main_checkout, monkeypatch):
    """Caso B: the ONLY legitimate main default — an attached main checkout with
    no explicit flag resolves to main / origin/main."""
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", main_checkout["work"])
    args = SimpleNamespace(branch=None)
    r = update_cmd._infer_update_branch(GIT, args)
    assert r is not None
    assert (r.branch, r.source_ref) == ("main", "origin/main")


def test_infer_tracked_custom_uses_real_tracking_ref(tracked_custom, monkeypatch):
    """Caso A: an attached tracked custom branch resolves to ITSELF with its
    REAL tracking ref (fork/<branch> — never a hardcoded origin, never main)."""
    work = tracked_custom["work"]
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", work)
    args = SimpleNamespace(branch=None)
    r = update_cmd._infer_update_branch(GIT, args)
    assert r is not None
    assert r.branch == CUSTOM
    assert r.source_ref == f"fork/{CUSTOM}"
    # Sanity: git agrees on the tracking ref.
    assert _tracking(work) == f"fork/{CUSTOM}"


# ---------------------------------------------------------------------------
# Caso A — E2E: custom tracked branch fast-forwards, stays, never main
# ---------------------------------------------------------------------------

def test_custom_tracked_branch_stays_ff_from_fork(tracked_custom, monkeypatch, capsys):
    """Tony's real case: branch before == branch after == custom; HEAD after ==
    the fork's custom tip; tracking preserved; main is NEVER checked out; the
    checkout is never left detached; main-only commits are NOT consumed."""
    work = tracked_custom["work"]
    head_before = _git(work, "rev-parse", "HEAD").stdout.strip()
    _patch_flow(monkeypatch, work)
    _stop_flow(monkeypatch)
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)

    with pytest.raises(_StopFlow):
        hermes_main.cmd_update(args)

    out = capsys.readouterr().out
    # Stayed on the custom branch, attached.
    assert _cur_branch(work) == CUSTOM
    # FF'd exactly to the fork's custom tip.
    assert _git(work, "rev-parse", "HEAD").stdout.strip() == tracked_custom["custom_tip"]
    assert _git(work, "rev-parse", "HEAD").stdout.strip() != head_before
    # Tracking preserved (rewriting it is forbidden).
    assert _tracking(work) == f"fork/{CUSTOM}"
    # Never checked out main.
    assert "checkout main" not in out
    assert "switching to main" not in out
    # main's exclusive commit was NOT pulled into the custom branch.
    not_ancestor = _git(work, "merge-base", "--is-ancestor",
                        tracked_custom["main_only_sha"], "HEAD", check=False)
    assert not_ancestor.returncode != 0


# ---------------------------------------------------------------------------
# Caso B — main estándar: comportamiento existente preservado
# ---------------------------------------------------------------------------

def test_main_checkout_updates_as_usual(main_checkout, monkeypatch):
    """Caso B: a standard main install (origin = the official repo) keeps the
    exact existing behavior — the legitimate ``main`` default, FF onto
    origin/main, ends on main."""
    work = main_checkout["work"]
    origin_tip = _git(main_checkout["origin"], "rev-parse", "refs/heads/main").stdout.strip()
    head_before = _git(work, "rev-parse", "HEAD").stdout.strip()
    _patch_flow(monkeypatch, work)
    _stop_flow(monkeypatch)
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)

    with pytest.raises(_StopFlow):
        hermes_main.cmd_update(args)

    assert _cur_branch(work) == "main"
    assert _git(work, "rev-parse", "HEAD").stdout.strip() == origin_tip
    assert _git(work, "rev-parse", "HEAD").stdout.strip() != head_before


# ---------------------------------------------------------------------------
# Caso C — detached HEAD, sin branch: fail safe, NUNCA main silencioso
# ---------------------------------------------------------------------------

def test_detached_without_branch_fails_safe_never_main(detached_head, monkeypatch, capsys):
    """Caso C: detached HEAD + no --branch -> the updater must fail safe (exit 1)
    with an actionable message and NEVER check out main. The Desktop side emits
    no --branch in this shape (see chooseUpdaterArgs vitest), so this is the
    exact argv the Desktop hands to ``hermes update``."""
    work = detached_head
    head_before = _git(work, "rev-parse", "HEAD").stdout.strip()
    _patch_flow(monkeypatch, work)
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)

    with pytest.raises(SystemExit) as exc_info:
        hermes_main.cmd_update(args)

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    # Safe-failure, not a silent main switch.
    assert "HEAD is detached" in out
    assert "never a silent main" in out
    assert "checkout main" not in out
    # The checkout is untouched: still detached at the same SHA.
    assert _cur_branch(work) == "HEAD"
    assert _git(work, "rev-parse", "HEAD").stdout.strip() == head_before


def test_detached_with_explicit_branch_reattaches_to_requested(
    detached_head, monkeypatch
):
    """Caso H (the only re-attach the contract allows): a UNAMBIGUOUS explicit
    --branch on a detached checkout re-attaches to THAT branch — and the Desktop
    only ever passes one when the user configured it. Here the explicit branch
    is main, so it lands on main; the point is 're-attach to the requested
    branch', driven by the flag, not by a synthesis."""
    work = detached_head
    # Advance origin/main so the update actually PULLS (an up-to-date checkout
    # would return before the stop point and verify nothing about the re-attach).
    seed = work.parent / "seed"
    _commit(seed, "m2.txt", "advance\n", "advance")
    _git(seed, "push", "-q", "origin", "main")
    _patch_flow(monkeypatch, work)
    _stop_flow(monkeypatch)
    args = SimpleNamespace(branch="main", yes=False, force=False, force_venv=False)

    with pytest.raises(_StopFlow):
        hermes_main.cmd_update(args)

    # Re-attached to the requested branch (no longer detached), at the remote tip.
    assert _cur_branch(work) == "main"
    assert _git(work, "rev-parse", "HEAD").stdout.strip() == _git(
        work, "rev-parse", "origin/main"
    ).stdout.strip()


# ---------------------------------------------------------------------------
# Caso D — rama sin upstream: fail safe estricto (NO_MAIN_FALLBACK_FOR_CUSTOM)
# ---------------------------------------------------------------------------

def test_custom_without_upstream_fails_safe_strict(custom_no_upstream, monkeypatch, capsys):
    """Caso D STRICT (Decisión A): attached to a non-main branch with no
    tracking upstream and no --branch -> safe failure. Hermes must NOT guess a
    remote and must NOT fall back to main."""
    work = custom_no_upstream
    _patch_flow(monkeypatch, work)
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)

    with pytest.raises(SystemExit) as exc_info:
        hermes_main.cmd_update(args)

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "local-only-feature" in out
    assert "no upstream" in out
    assert "NO_MAIN_FALLBACK_FOR_CUSTOM" in out
    assert "checkout main" not in out
    # The branch is untouched.
    assert _cur_branch(work) == "local-only-feature"


def test_custom_missing_from_remote_fails_safe_config_unchanged(
    tracked_custom, monkeypatch, capsys
):
    """Design correction point 2: the local custom branch still configures a
    tracking upstream, but the remote ref was deleted -> the fetch fails. The
    updater must fail safe with the real branch context and NOT heal to main:
    the tracking config is untouched and no main checkout happens."""
    work = tracked_custom["work"]
    # Kill the remote ref (the branch 'disappeared' on the fork).
    _git(tracked_custom["fork"], "update-ref", "-d", f"refs/heads/{CUSTOM}", check=False)
    head_before = _git(work, "rev-parse", "HEAD").stdout.strip()

    _patch_flow(monkeypatch, work)
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)

    with pytest.raises(SystemExit) as exc_info:
        hermes_main.cmd_update(args)

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    # Still the real branch — no synthesis to main, tracking config intact.
    assert _cur_branch(work) == CUSTOM
    assert _tracking(work) == f"fork/{CUSTOM}"
    assert (work / ".git").exists()
    remote_cfg = _git(work, "config", "branch.%s.remote" % CUSTOM).stdout.strip()
    assert remote_cfg == "fork"
    # No main checkout, nothing pulled.
    assert "checkout main" not in out
    assert _git(work, "rev-parse", "HEAD").stdout.strip() == head_before


# ---------------------------------------------------------------------------
# Caso E — dirty tree: nunca descartar cambios
# ---------------------------------------------------------------------------

def test_dirty_tracked_custom_update_preserves_local_changes(
    tracked_custom, monkeypatch
):
    """Caso E: an uncommitted edit on the tracked custom branch must survive the
    update — the updater autostashes, FFs from the fork ref and restores the
    edit. Nothing is silently discarded and the branch never moves."""
    work = tracked_custom["work"]
    # A tracked-file edit (rides the autostash) + an untracked scratch file.
    (work / "a.txt").write_text("local edit\n")
    (work / "scratch.py").write_text("wip\n")
    edit_blob = (work / "a.txt").read_text()

    _patch_flow(monkeypatch, work)
    _stop_flow(monkeypatch)
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)

    with pytest.raises(_StopFlow):
        hermes_main.cmd_update(args)

    # The FF happened (remote content present) AND the local edit came back.
    assert _cur_branch(work) == CUSTOM
    assert _git(work, "rev-parse", "HEAD").stdout.strip() == tracked_custom["custom_tip"]
    assert (work / "c.txt").read_text() == "custom-advance\n"
    assert (work / "a.txt").read_text() == edit_blob
    # No leftover stash — the restore settled it.
    assert _git(work, "stash", "list").stdout.strip() == ""


# ---------------------------------------------------------------------------
# FF-only — divergence en rama custom trackeada: fail safe, sin merge/reset
# ---------------------------------------------------------------------------

def test_diverged_tracked_custom_fails_safe_no_merge_no_reset(
    tracked_custom, monkeypatch, capsys
):
    """Caso F / §4: the tracked custom branch has a LOCAL commit the fork does
    not have while the fork advanced -> not fast-forwardable. The updater must
    FAIL SAFE (no merge commit, no rebase, no reset); local work stays exactly
    where it was, still on the branch."""
    work = tracked_custom["work"]
    _commit(work, "local.txt", "local-only-work\n", "local-only")
    local_sha = _git(work, "rev-parse", "HEAD").stdout.strip()
    branch_sha_before = local_sha

    _patch_flow(monkeypatch, work)
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)

    with pytest.raises(SystemExit) as exc_info:
        hermes_main.cmd_update(args)

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "diverged from its tracking ref" in out
    assert "No merge commit, rebase, or reset was performed" in out
    # HEAD untouched: still at the local commit (no reset to the remote tip).
    assert _cur_branch(work) == CUSTOM
    assert _git(work, "rev-parse", "HEAD").stdout.strip() == local_sha
    # The branch ref still points at the local commit (no reset --hard moved it).
    assert _git(work, "rev-parse", CUSTOM).stdout.strip() == branch_sha_before
    # Not a merge: single-parent history, no new merge commit.
    assert _git(work, "rev-list", "--count", f"{branch_sha_before}..HEAD").stdout.strip() == "0"
    # Still tracking the fork ref.
    assert _tracking(work) == f"fork/{CUSTOM}"


# ---------------------------------------------------------------------------
# Caso A (up to date) — custom trackeada sin commits nuevos: no-op seguro
# ---------------------------------------------------------------------------

def test_tracked_custom_up_to_date_is_a_safe_noop(tracked_custom, monkeypatch, capsys):
    """Caso A, zero-commit shape: the tracked custom branch is already at the
    fork tip -> the update is a safe no-op: same branch, same HEAD, tracking
    intact, no main checkout, no rebuild churn."""
    work = tracked_custom["work"]
    # The fixture leaves the fork's custom branch one commit ahead; fast-forward
    # the work copy to the tip so this test exercises the TRUE zero-commit shape.
    _git(work, "fetch", "-q", "fork", CUSTOM)
    _git(work, "merge", "-q", "--ff-only", f"fork/{CUSTOM}")
    head_before = _git(work, "rev-parse", "HEAD").stdout.strip()
    _patch_flow(monkeypatch, work)
    # The zero-commit path returns via _finish_already_up_to_date; stop there —
    # its tail (venv repair / fleet catch-up) is out of scope for this contract.
    class _StopFinish(Exception):
        pass

    monkeypatch.setattr(
        update_cmd,
        "_finish_already_up_to_date",
        lambda *a, **k: (_ for _ in ()).throw(_StopFinish()),
    )
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)

    with pytest.raises(_StopFinish):
        hermes_main.cmd_update(args)

    assert _cur_branch(work) == CUSTOM
    assert _git(work, "rev-parse", "HEAD").stdout.strip() == head_before
    assert _tracking(work) == f"fork/{CUSTOM}"
    out = capsys.readouterr().out
    assert "checkout main" not in out
