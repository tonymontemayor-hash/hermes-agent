"""Goal auto-continue after ``max_iterations_reached`` — 9 mandatory tests.

Fixes: a goal work turn that reaches ``max_iterations`` and produces a summary
was rejected by ``_is_successful_goal_turn`` (``completed=False``), the judge
was never run, and the goal stayed active/incomplete without a continuation.

The minimal fix: ``max_iterations_reached`` with a non-empty response IS
resumable — the judge must decide (continue / done / blocked).  Only hard
failures (``failed=True``) and ``completed=False`` WITHOUT a
``max_iterations_reached`` reason remain non-resumable.

Tests 1-9 (mandatory):
  1. work turn reaches max_iterations -> goal incomplete -> next work turn
     auto-dispatched (``_goal_followup_after_turn`` returns a continuation)
  2. turn 2 can continue from checkpoint (``evaluate_after_turn`` turn 2)
  3. repeated max_iterations can continue through >2 goal turns
  4. goal done after a later turn stops correctly
  5. blocked stops correctly
  6. cancelled (cleared) stops correctly
  7. goals.max_turns=50 still caps continuation
  8. ordinary non-goal max_iterations behavior unchanged
  9. no busy-loop if continuation dispatch itself fails
"""
from __future__ import annotations

import threading
import types

import pytest

from tui_gateway import server
from hermes_cli import goals as goals_mod


# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point the goals SessionDB at an isolated HERMES_HOME."""
    import hermes_state  # noqa: F401
    goals_mod._DB_CACHE.clear()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    goals_mod._DB_CACHE.clear()
    yield tmp_path / ".hermes"
    goals_mod._DB_CACHE.clear()


def _session(agent=None):
    return {
        "agent": agent if agent is not None else types.SimpleNamespace(session_id="test-session"),
        "session_key": "test-session",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
        "inflight_turn": None,
    }


class _InlineThread:
    """Run threads synchronously so tests observe final state."""

    def __init__(self, target=None, daemon=None, args=(), kwargs=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


def _max_iterations_result(summary: str = "Progress: turns 1-250 done.") -> dict:
    """A result dict for a turn that hit ``max_iterations`` with a summary."""
    return {
        "final_response": summary,
        "messages": [{"role": "user", "content": "do the goal"}],
        "api_calls": 250,
        "completed": False,
        "failed": False,
        "turn_exit_reason": "max_iterations_reached(250/250)",
        "partial": False,
        "interrupted": False,
        "model": "test-model",
        "provider": "test",
        "base_url": "http://localhost",
    }


def _normal_result(summary: str = "Done with the task.") -> dict:
    return {
        "final_response": summary,
        "messages": [{"role": "user", "content": "do the task"}],
        "api_calls": 5,
        "completed": True,
        "failed": False,
        "turn_exit_reason": "text_response(finish_reason=stop)",
        "partial": False,
        "interrupted": False,
        "model": "test-model",
        "provider": "test",
        "base_url": "http://localhost",
    }


def _failed_result() -> dict:
    return {
        "final_response": "API error: 500",
        "messages": [],
        "api_calls": 1,
        "completed": False,
        "failed": True,
        "turn_exit_reason": "text_response(finish_reason=stop)",
        "partial": False,
        "interrupted": False,
        "error": "API error: 500",
        "model": "test-model",
        "provider": "test",
        "base_url": "http://localhost",
    }


def _patch_env(monkeypatch, verdict="continue", reason="still in progress"):
    """Neutralize the turn pipeline's environment-heavy side paths and
    patch the goal judge to return the given verdict."""
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(
        server, "_load_cfg",
        lambda: {"goals": {"max_turns": 50}},
    )
    monkeypatch.setattr(
        server, "_plan_goal_compression_recovery",
        lambda session, result, *, status, raw: (None, None),
    )
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda *a, **k: False)
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(
        goals_mod, "judge_goal",
        lambda *a, **k: (verdict, reason, False, None, False),
    )
    monkeypatch.setattr(goals_mod.GoalManager, "_check_gates", lambda self: None)
    monkeypatch.setattr(
        goals_mod, "gather_background_processes",
        lambda owner_task_id=None: [],
    )
    monkeypatch.setattr(
        goals_mod, "count_active_delegations",
        lambda session_id=None: 0,
    )


def _patch_active_goal(monkeypatch, goal_mgr):
    monkeypatch.setattr(server, "_active_goal_manager", lambda session: goal_mgr)


def _new_goal_manager(session_id: str = "test-session", max_turns: int = 50):
    """Create a fresh GoalManager with an active goal (no contract)."""
    mgr = goals_mod.GoalManager(session_id=session_id, default_max_turns=max_turns)
    mgr.set(f"goal for {session_id}")
    return mgr


# ── 1. work turn reaches max_iterations -> auto-dispatched ──────────


def test_1_max_iterations_auto_dispatch(isolated_home, monkeypatch):
    """A goal work turn that reaches ``max_iterations`` and produces a summary
    triggers the goal judge, which (saying 'continue') returns a continuation
    prompt — the next work turn is auto-dispatched."""
    _patch_env(monkeypatch, verdict="continue")
    mgr = _new_goal_manager("sess-1")
    _patch_active_goal(monkeypatch, mgr)

    session = _session()
    result = _max_iterations_result()
    followup = server._goal_followup_after_turn(
        "sid-1", session, result, "complete", result["final_response"])

    assert followup is not None, "max_iterations turn must produce a continuation"
    assert "Continuing toward" in followup
    assert mgr.state.turns_used == 1


# ── 2. turn 2 can continue from checkpoint ──────────────────────────


def test_2_turn2_continues_from_checkpoint(isolated_home, monkeypatch):
    """Turn 2 (another ``max_iterations`` turn) continues from the checkpoint
    saved by turn 1."""
    _patch_env(monkeypatch, verdict="continue")
    mgr = _new_goal_manager("sess-2")
    _patch_active_goal(monkeypatch, mgr)

    session = _session()
    result = _max_iterations_result()
    for turn in (1, 2):
        followup = server._goal_followup_after_turn(
            "sid-2", session, result, "complete", result["final_response"])
        assert followup is not None, f"turn {turn} should continue"

    assert mgr.state.turns_used == 2
    assert mgr.state.status == "active"


# ── 3. repeated max_iterations through >2 goal turns ────────────────


def test_3_repeated_max_iterations_multi_turn(isolated_home, monkeypatch):
    """Three consecutive ``max_iterations`` turns each produce a continuation
    (>2 goal turns of repeated max_iterations)."""
    _patch_env(monkeypatch, verdict="continue")
    mgr = _new_goal_manager("sess-3")
    _patch_active_goal(monkeypatch, mgr)

    session = _session()
    result = _max_iterations_result()
    for turn in (1, 2, 3):
        followup = server._goal_followup_after_turn(
            "sid-3", session, result, "complete", result["final_response"])
        assert followup is not None, f"turn {turn} should continue"

    assert mgr.state.turns_used == 3
    assert mgr.state.status == "active"


# ── 4. goal done after a later turn stops correctly ─────────────────


def test_4_goal_done_stops(isolated_home, monkeypatch):
    """After a ``max_iterations`` turn, the judge says 'done': the goal is
    marked done and no continuation is produced."""
    _patch_env(monkeypatch, verdict="done", reason="all work complete")
    mgr = _new_goal_manager("sess-4")
    _patch_active_goal(monkeypatch, mgr)

    session = _session()
    result = _max_iterations_result()
    followup = server._goal_followup_after_turn(
        "sid-4", session, result, "complete", result["final_response"])

    assert followup is None, "done goal must not produce a continuation"
    assert mgr.state.status == "done"


# ── 5. blocked stops correctly ──────────────────────────────────────


def test_5_blocked_stops(isolated_home, monkeypatch):
    """After a ``max_iterations`` turn, the judge says 'blocked': the goal is
    paused and no continuation is produced."""
    _patch_env(monkeypatch, verdict="blocked", reason="needs user input")
    mgr = _new_goal_manager("sess-5")
    _patch_active_goal(monkeypatch, mgr)

    session = _session()
    result = _max_iterations_result()
    followup = server._goal_followup_after_turn(
        "sid-5", session, result, "complete", result["final_response"])

    assert followup is None, "blocked goal must not produce a continuation"
    assert mgr.state.status == "paused"


# ── 6. cancelled (cleared) stops correctly ──────────────────────────


def test_6_cancelled_stops(isolated_home, monkeypatch):
    """After a ``max_iterations`` turn, the goal is cleared: no continuation
    is produced and the goal is not active."""
    _patch_env(monkeypatch, verdict="continue")
    mgr = _new_goal_manager("sess-6")
    mgr.clear()  # user cancels
    assert mgr.state is None or mgr.state.status != "active"
    _patch_active_goal(monkeypatch, None)  # no active goal

    session = _session()
    result = _max_iterations_result()
    followup = server._goal_followup_after_turn(
        "sid-6", session, result, "complete", result["final_response"])

    assert followup is None, "cleared goal must not produce a continuation"


# ── 7. goals.max_turns=50 still caps continuation ───────────────────


def test_7_budget_50_still_enforced(isolated_home, monkeypatch):
    """With ``goals.max_turns=50``, 50 consecutive ``max_iterations`` turns
    all continue, but the 51st is a budget pause."""
    _patch_env(monkeypatch, verdict="continue")
    mgr = _new_goal_manager("sess-7", max_turns=50)
    _patch_active_goal(monkeypatch, mgr)

    session = _session()
    result = _max_iterations_result()
    last = None
    for turn in range(1, 52):
        last = server._goal_followup_after_turn(
            "sid-7", session, result, "complete", result["final_response"])

    # Turn 50: still active (turns_used == 50 == max_turns, but the judge
    # already ran and turns_used >= max_turns → budget pause)
    # Actually evaluate_after_turn checks turns_used >= max_turns AFTER
    # incrementing, so turn 50 → turns_used=50 >= 50 → budget pause.
    assert last is None, f"turn 50 should be a budget pause, got: {last!r}"
    assert mgr.state.status == "paused"
    assert "budget" in (mgr.state.paused_reason or "").lower()


# ── 8. ordinary non-goal max_iterations behavior unchanged ──────────


def test_8_non_goal_max_iterations_unchanged(isolated_home, monkeypatch):
    """Without an active goal, a ``max_iterations`` turn does NOT trigger
    the goal judge and returns no continuation."""
    _patch_env(monkeypatch, verdict="continue")
    _patch_active_goal(monkeypatch, None)  # no active goal

    session = _session()
    result = _max_iterations_result()
    followup = server._goal_followup_after_turn(
        "sid-8", session, result, "complete", result["final_response"])

    assert followup is None, "non-goal max_iterations must not produce a continuation"


# ── 9. no busy-loop if continuation dispatch fails ──────────────────


def test_9_no_busy_loop_on_dispatch_failure(isolated_home, monkeypatch):
    """If ``_dispatch_followup_turn`` raises, ``_run_post_turn_followups``
    catches it (no unhandled exception, no infinite retry) and the session's
    ``running`` flag is released so the goal cannot spin in a busy-loop."""
    _patch_env(monkeypatch, verdict="continue")
    session = _session()
    result = _normal_result()

    # Simulate the dispatch target failing (e.g. session evicted / backend
    # crash mid-dispatch).  _dispatch_followup_turn's own try/except is what
    # releases running — patch _run_prompt_submit to raise and keep the real
    # dispatcher in place.
    def _raise_submit(*a, **k):
        raise RuntimeError("dispatch failed")

    monkeypatch.setattr(server, "_run_prompt_submit", _raise_submit)

    # running must start False so _run_post_turn_followups SETS it True and
    # dispatches; a dispatch failure must then release it back to False.
    session["running"] = False
    try:
        server._run_post_turn_followups("rid-9", "sid-9", session, result, "goal continuation")
    except Exception:
        pytest.fail("_run_post_turn_followups must not raise on dispatch failure")

    assert session["running"] is False, "running must be released after dispatch failure"


# ── _is_successful_goal_turn unit tests ─────────────────────────────


def test_gate_max_iterations_resumable():
    """A ``max_iterations_reached`` turn with a summary IS resumable."""
    result = _max_iterations_result()
    assert server._is_successful_goal_turn(
        result, "complete", result["final_response"]) is True


def test_gate_completed_false_without_max_iterations_not_resumable():
    """A ``completed=False`` turn WITHOUT ``max_iterations_reached`` is NOT
    resumable (e.g. compression_deferred, error)."""
    result = {
        "final_response": "deferred",
        "messages": [],
        "api_calls": 1,
        "completed": False,
        "failed": False,
        "turn_exit_reason": "compression_deferred",
        "partial": False,
        "interrupted": False,
    }
    assert server._is_successful_goal_turn(result, "complete", "deferred") is False


def test_gate_failed_not_resumable():
    """A failed turn is NOT resumable regardless of ``turn_exit_reason``."""
    result = _failed_result()
    assert server._is_successful_goal_turn(result, "complete", "API error: 500") is False


def test_gate_normal_complete_turn_resumable():
    """A normal ``completed=True`` turn is resumable (unchanged behavior)."""
    result = _normal_result()
    assert server._is_successful_goal_turn(
        result, "complete", result["final_response"]) is True


def test_gate_empty_response_not_resumable():
    """A turn with an empty response is NOT resumable."""
    result = _max_iterations_result(summary="")
    assert server._is_successful_goal_turn(result, "complete", "") is False


def test_gate_non_dict_result():
    """A non-dict result (str) is resumable if status is complete and
    the response is non-empty."""
    assert server._is_successful_goal_turn("some text", "complete", "some text") is True
    assert server._is_successful_goal_turn("some text", "error", "some text") is False
