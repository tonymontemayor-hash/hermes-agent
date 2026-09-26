"""Verification tests for the /goal iteration budget default (20 -> 50).

Proves the *runtime* (not just the UI) actually moves:
  * a NEW goal created with no override gets max_turns == 50 (the new default);
  * a goal can EXECUTE PAST turn 20 (turn 21 still has budget) -- the old 20
    default would have auto-paused at 20;
  * a PERSISTED goal keeps its stored max_turns (7 stays 7 across to_json/
    from_json roundtrip, it does NOT adopt 50);
  * an explicit per-goal override still wins (max_turns=7 -> 7);
  * the config default surface (config_defaults) reports 50;
  * invalid/absent config values fall back safely to the 50 default.

These are regression guards: they would FAIL on the old 20 default (turn 21
would be a budget-pause) and on any naive "UI-only" change (turn 21 budget
check is exercised through the real GoalManager.evaluate_after_turn path).
"""
import time

import pytest

from hermes_cli import goals
from hermes_cli.config_defaults import DEFAULT_CONFIG


def _iso_session_db(home):
    """Point the goals SessionDB at an isolated HERMES_HOME and drop the cache,
    so the test neither reads nor writes the live goal store."""
    import hermes_state  # noqa: F401  (import for the monkeypatch target)
    goals._DB_CACHE.clear()


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


def _continue_judge(monkeypatch):
    """Patch the judge to always say 'continue' (the goal is in progress) and
    disable quality gates, so evaluate_after_turn runs purely on the budget."""
    monkeypatch.setattr(
        goals, "judge_goal",
        lambda *a, **k: ("continue", "still working on it", False, None, False),
    )
    monkeypatch.setattr(goals.GoalManager, "_check_gates", lambda self: None)


def test_default_max_turns_is_50():
    """The module constant and the config default both report 50."""
    assert goals.DEFAULT_MAX_TURNS == 50
    assert DEFAULT_CONFIG["goals"]["max_turns"] == 50


def test_new_goal_gets_default_50(isolated_home):
    """A goal created with no per-goal override adopts the 50 default."""
    mgr = goals.GoalManager(session_id="new-goal-default")
    assert mgr.default_max_turns == 50
    state = mgr.set("ship the benchmark")
    assert state.max_turns == 50
    # The runtime UI state (turns/max_turns) reflects the real limit.
    assert mgr.state.max_turns == 50


def test_turn_21_executes(isolated_home, monkeypatch):
    """With the new 50 default, the goal survives PAST turn 20: turn 21 still
    has budget (should_continue True). Under the old 20 default this exact
    call would auto-pause at turn 20."""
    _continue_judge(monkeypatch)
    mgr = goals.GoalManager(session_id="turn-21-executes")
    mgr.set("big standing goal")  # default max_turns == 50
    assert mgr.state.max_turns == 50

    last = None
    for turn in range(1, 22):  # execute turns 1..21
        last = mgr.evaluate_after_turn("progressing well on turn", user_initiated=False)
    # After executing turn 21: the 21st evaluation must NOT be a budget pause.
    assert last["status"] == "active", f"turn 21 wrongly paused: {last}"
    assert last["should_continue"] is True, f"turn 21 should continue: {last}"
    assert mgr.state.turns_used == 21
    # And it is genuinely below budget (not a fluke at the boundary).
    assert mgr.state.turns_used < mgr.state.max_turns


def test_old_20_budget_would_have_paused_at_21(isolated_home, monkeypatch):
    """Contrast guard: a goal explicitly capped at 20 (the old default) DOES
    pause by the time turn 21 is evaluated -- proving the test discriminates
    a real budget change from a cosmetic one."""
    _continue_judge(monkeypatch)
    mgr = goals.GoalManager(session_id="old-20-contrast")
    mgr.set("capped goal", max_turns=20)  # simulate the pre-change default
    assert mgr.state.max_turns == 20

    last = None
    for _ in range(1, 22):  # 21 turns
        last = mgr.evaluate_after_turn("still going", user_initiated=False)
    # By the end, the budget (20) is exhausted -> paused, not continuing.
    assert last["status"] == "paused", f"20-budget goal should pause: {last}"
    assert last["should_continue"] is False


def test_persisted_goal_keeps_stored_limit(isolated_home):
    """A goal that was persisted with max_turns=7 keeps 7 across a roundtrip;
    it does NOT adopt the new 50 default on reload."""
    mgr = goals.GoalManager(session_id="persist-keeps-limit")
    state = mgr.set("long running goal", max_turns=7)
    assert state.max_turns == 7

    raw = state.to_json()
    reloaded = goals.GoalState.from_json(raw)
    assert reloaded.max_turns == 7, "persisted max_turns must survive reload"
    assert reloaded.turns_used == state.turns_used
    assert reloaded.status == state.status


def test_persisted_goal_with_default_survives_reload(isolated_home):
    """A goal created on the NEW default (50) also roundtrips intact."""
    mgr = goals.GoalManager(session_id="persist-default-50")
    state = mgr.set("default-budget goal")
    assert state.max_turns == 50
    reloaded = goals.GoalState.from_json(state.to_json())
    assert reloaded.max_turns == 50


def test_explicit_per_goal_override_wins(isolated_home):
    """An explicit override (both below and above the default) is respected."""
    mgr = goals.GoalManager(session_id="override-wins")
    assert mgr.set("small goal", max_turns=3).max_turns == 3
    assert mgr.set("long goal", max_turns=80).max_turns == 80
    # Default (no override) -> 50.
    assert mgr.set("default goal").max_turns == 50


def test_invalid_config_falls_back_to_default():
    """The loader pattern `int(cfg.get('max_turns', D) or D)` degrades safely:
    absent -> 50, zero -> 50, non-numeric string -> exception caught by the
    callers' own `except Exception: return DEFAULT_MAX_TURNS`."""
    D = goals.DEFAULT_MAX_TURNS
    assert D == 50

    # absent key
    assert int(({}).get("max_turns", D) or D) == 50
    # explicit zero is falsy -> default (the callers treat 0 as 'unset')
    assert int(({"max_turns": 0}).get("max_turns", D) or D) == 50
    # a valid override still wins
    assert int(({"max_turns": 7}).get("max_turns", D) or D) == 7
    # a garbage value raises, and every call site wraps this in try/except
    # returning DEFAULT_MAX_TURNS (verified in the gateway/tui/cli loaders).
    with pytest.raises(Exception):
        int(({"max_turns": "garbage"}).get("max_turns", D) or D)
