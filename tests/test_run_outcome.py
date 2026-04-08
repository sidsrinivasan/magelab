import pytest

from magelab.orchestrator import RunOutcome


# ---------------------------------------------------------------------------
# 1. Enum values — all 5 exist with correct string values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "member, expected_value",
    [
        (RunOutcome.NO_WORK, "no_work"),
        (RunOutcome.SUCCESS, "success"),
        (RunOutcome.PARTIAL, "partial"),
        (RunOutcome.FAILURE, "failure"),
        (RunOutcome.TIMEOUT, "timeout"),
    ],
)
def test_enum_string_values(member, expected_value):
    assert member.value == expected_value


# ---------------------------------------------------------------------------
# 2. String behavior — RunOutcome is (str, Enum)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "member, raw_string",
    [
        (RunOutcome.NO_WORK, "no_work"),
        (RunOutcome.SUCCESS, "success"),
        (RunOutcome.PARTIAL, "partial"),
        (RunOutcome.FAILURE, "failure"),
        (RunOutcome.TIMEOUT, "timeout"),
    ],
)
def test_string_comparison(member, raw_string):
    # Because RunOutcome inherits from str, direct equality with the raw
    # string value should hold.
    assert member == raw_string


# ---------------------------------------------------------------------------
# 3. exit_code property
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome, expected_code",
    [
        (RunOutcome.SUCCESS, 0),
        (RunOutcome.NO_WORK, 0),
        (RunOutcome.PARTIAL, 1),
        (RunOutcome.TIMEOUT, 2),
        (RunOutcome.FAILURE, 3),
    ],
)
def test_exit_code(outcome, expected_code):
    assert outcome.exit_code == expected_code


# ---------------------------------------------------------------------------
# 4. from_exit_code class method
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code, expected_outcome",
    [
        (0, RunOutcome.SUCCESS),
        (1, RunOutcome.PARTIAL),
        (2, RunOutcome.TIMEOUT),
        (3, RunOutcome.FAILURE),
        # Unknown codes → FAILURE
        (42, RunOutcome.FAILURE),
        (-1, RunOutcome.FAILURE),
        (255, RunOutcome.FAILURE),
    ],
)
def test_from_exit_code(code, expected_outcome):
    assert RunOutcome.from_exit_code(code) == expected_outcome


def test_from_exit_code_returns_run_outcome_instance():
    for code in (0, 1, 2, 3, 99):
        result = RunOutcome.from_exit_code(code)
        assert isinstance(result, RunOutcome)


# ---------------------------------------------------------------------------
# 5. Round-trip: from_exit_code(outcome.exit_code) is a valid RunOutcome
#    Note: NO_WORK.exit_code == 0 → from_exit_code(0) == SUCCESS (first match)
#    This asymmetry is expected and documented.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", list(RunOutcome))
def test_round_trip_returns_valid_outcome(outcome):
    result = RunOutcome.from_exit_code(outcome.exit_code)
    assert isinstance(result, RunOutcome)


def test_round_trip_no_work_maps_to_success():
    # Both NO_WORK and SUCCESS map to exit code 0; from_exit_code returns
    # SUCCESS as the first match, so NO_WORK does not round-trip to itself.
    result = RunOutcome.from_exit_code(RunOutcome.NO_WORK.exit_code)
    assert result == RunOutcome.SUCCESS


@pytest.mark.parametrize(
    "outcome",
    [
        RunOutcome.SUCCESS,
        RunOutcome.PARTIAL,
        RunOutcome.TIMEOUT,
        RunOutcome.FAILURE,
    ],
)
def test_round_trip_exact_for_non_no_work(outcome):
    # All outcomes except NO_WORK should round-trip back to themselves.
    assert RunOutcome.from_exit_code(outcome.exit_code) == outcome


# ---------------------------------------------------------------------------
# 6. All exit codes are in range 0-3
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", list(RunOutcome))
def test_exit_code_in_valid_range(outcome):
    assert 0 <= outcome.exit_code <= 3


# ---------------------------------------------------------------------------
# 7. EXIT_CODE_MAP completeness — every enum member has a mapping
# ---------------------------------------------------------------------------


def test_exit_code_map_covers_all_members():
    """Every RunOutcome member must have an exit_code (i.e., exist in _EXIT_CODE_MAP).
    If a new member is added without updating the map, this fails with KeyError."""
    for outcome in RunOutcome:
        # .exit_code accesses _EXIT_CODE_MAP[self] — KeyError if missing
        assert isinstance(outcome.exit_code, int)
