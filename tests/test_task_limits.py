"""SYNTHETIC per-keyword target, persistent policy, and 500-record execution tests.

The long run uses the existing synthetic Android state machine and a virtual
clock. It operates no real phone, and its evidence is never a collected result.
"""

import pytest
from sqlalchemy import create_engine
from test_runner_recovery import FIXTURES, Clock, SyntheticPhone

from xhs_mobile.config import Policy, Settings, TaskLimits, load_settings, validate_target
from xhs_mobile.evidence import EvidenceStore
from xhs_mobile.models import Base
from xhs_mobile.profile import load_profile
from xhs_mobile.repository import Repository
from xhs_mobile.runner import Runner


@pytest.mark.parametrize(("target", "details", "swipes"), [
    (1, 100, 50), (10, 100, 50), (100, 300, 150), (101, 303, 152), (500, 1500, 750),
])
def test_target_generates_bounded_policy_and_preserves_non_budget_options(
    target, details, swipes,
):
    original = Policy(action_interval=5, page_timeout=45, max_retries=1,
                      cooldown_seconds=2100, max_no_progress=4)
    settings = Settings(policy=original)
    generated = settings.new_task_policy(target)
    assert generated.max_detail_visits == details
    assert generated.max_list_swipes == swipes
    ignored = {"max_detail_visits", "max_list_swipes"}
    assert generated.model_dump(exclude=ignored) == original.model_dump(exclude=ignored)
    assert settings.policy.max_detail_visits == 100
    assert settings.policy.max_list_swipes == 50
    assert generated is not original


@pytest.mark.parametrize("target", [0, -1, 501, True, False, 100.0, 1.5, "500", None])
def test_target_boundaries_are_strict_before_task_creation(target):
    with pytest.raises(ValueError, match="1 至 500"):
        validate_target(target)
    with pytest.raises(ValueError, match="1 至 500"):
        Settings().new_task_policy(target)


@pytest.mark.parametrize("cap", [
    {"max_detail_visits": 299}, {"max_list_swipes": 149},
])
def test_operator_creation_caps_reject_target_before_any_storage(cap):
    settings = Settings(task_limits=TaskLimits(**cap))
    with pytest.raises(ValueError, match="task_limits"):
        settings.new_task_policy(100)


@pytest.mark.parametrize("cap", [
    {"max_detail_visits": 1501}, {"max_list_swipes": 751},
    {"max_detail_visits": True}, {"max_list_swipes": 750.0},
])
def test_creation_hard_caps_cannot_be_accidentally_exceeded(cap):
    with pytest.raises(ValueError):
        TaskLimits(**cap)


def test_legacy_configuration_supports_new_500_target_without_rewriting_file(tmp_path):
    path = tmp_path / "synthetic-legacy.toml"
    old_content = "[policy]\nmax_detail_visits = 100\nmax_list_swipes = 50\n"
    path.write_text(old_content)
    settings = load_settings(path)
    policy = settings.new_task_policy(500)
    assert (policy.max_detail_visits, policy.max_list_swipes) == (1500, 750)
    assert path.read_text() == old_content


def test_synthetic_runner_reaches_500_with_resume_and_original_limits_intact(tmp_path):
    """Real Runner traverses 250 synthetic viewports and stops at exactly 500."""
    engine = create_engine(f"sqlite:///{tmp_path / 'SYNTHETIC-500.sqlite'}")
    Base.metadata.create_all(engine)
    repo = Repository(engine)
    clock = Clock()
    phone = SyntheticPhone(clock)
    ids = [f"{index:024x}" for index in range(1, 501)]
    phone.result_pages = [ids[index:index + 2] for index in range(0, len(ids), 2)]
    profile = load_profile(FIXTURES / "synthetic_profile.toml", require_verified=False)
    profile.actions.pop("image_text_filter")
    profile.actions.pop("back_to_results")
    policy = Settings().new_task_policy(500)
    task_id = repo.create_task(
        device_id="SYNTHETIC-device", serial=phone.serial,
        session_ref="SYNTHETIC-session", keyword="SYNTHETIC 500 target",
        target=500, policy=policy.model_dump(),
    ).id
    old_id = repo.create_task(
        device_id="SYNTHETIC-device", serial=phone.serial,
        session_ref="SYNTHETIC-session", keyword="SYNTHETIC legacy task",
        target=10, policy=Policy().model_dump(),
    ).id
    repo.consume(old_id, "detail_visits", 100)
    repo.consume(old_id, "list_swipes", 50)
    evidence = EvidenceStore(tmp_path / "SYNTHETIC-evidence")

    def run(stop_requested=lambda: False):
        return Runner(repository=repo, device=phone, evidence=evidence, profile=profile,
                      sleep=clock.sleep, monotonic=clock.monotonic, now=clock.now,
                      stop_requested=stop_requested).execute(task_id)

    try:
        # An actual committed checkpoint, followed by a fresh Runner, must retain
        # the 500 target and generated budgets instead of reloading old 100/50.
        stopped = run(lambda: repo.task(task_id).eligible_count >= 100)
        assert stopped["tasks"][0]["status"] == "paused"
        checkpoint = repo.task(task_id)
        assert checkpoint.eligible_count == 100
        assert checkpoint.detail_visits == 100
        first_swipes = checkpoint.list_swipes
        clock.sleep(3)
        repo = Repository(engine)
        finished = run()["tasks"][0]
        assert finished["status"] == "collected_awaiting_review", finished
        assert finished["eligible_count"] == finished["observation_count"] == 500
        assert finished["known_unique"] == 500
        assert finished["human_verified_unique"] == 0
        assert finished["stop_reason"] == "target_collected"
        persisted = repo.task(task_id)
        assert persisted.target == 500 and persisted.policy == policy.model_dump()
        # Recovery deliberately repeats navigation; committed identities dedupe.
        assert 500 <= persisted.detail_visits < 1500
        assert first_swipes < persisted.list_swipes < 750
        assert len(set(phone.opened_note_ids)) == 500
        assert all(b[0] - a[0] >= 3 for a, b in zip(phone.writes, phone.writes[1:], strict=False))
        legacy = repo.task(old_id)
        assert legacy.target == 10 and legacy.policy == Policy().model_dump()
        assert (legacy.detail_visits, legacy.list_swipes) == (1, 1)
    finally:
        engine.dispose()
