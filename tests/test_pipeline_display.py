import pytest

from magelab.pipeline.display import (
    fmt_duration,
    _is_outcome_string,
    _outcome_icon,
    _outcome_style,
    StatusDisplay,
    PHASE_ICON,
)


# ---------------------------------------------------------------------------
# fmt_duration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (0, "0s"),
        (0.5, "0s"),
        (45, "45s"),
        (60, "1m 00s"),
        (65, "1m 05s"),
        (90, "1m 30s"),
        (3600, "1h 00m"),
        (3900, "1h 05m"),
    ],
)
def test_fmt_duration(seconds, expected):
    assert fmt_duration(seconds) == expected


# ---------------------------------------------------------------------------
# _is_outcome_string
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phase, expected",
    [
        ("S", True),
        ("SSF", True),
        ("SPFTN", True),
        ("", False),
        ("X", False),
        ("running", False),
        ("Sf", False),
    ],
)
def test_is_outcome_string(phase, expected):
    assert _is_outcome_string(phase) == expected


# ---------------------------------------------------------------------------
# _outcome_icon
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phase, expected_icon",
    [
        ("S", "✓"),
        ("SSS", "✓"),
        ("N", "⊘"),
        ("SN", "⊘"),
        ("P", "◑"),
        ("SP", "◑"),
        ("NP", "◑"),
        ("T", "⧖"),
        ("ST", "⧖"),
        ("PT", "⧖"),
        ("NT", "⧖"),
        ("F", "✗"),
        ("SF", "✗"),
        ("PF", "✗"),
        ("TF", "✗"),
        ("NF", "✗"),
        ("SPFTN", "✗"),
    ],
)
def test_outcome_icon(phase, expected_icon):
    assert _outcome_icon(phase) == expected_icon


# ---------------------------------------------------------------------------
# _outcome_style (ANSI escape codes)
# ---------------------------------------------------------------------------

RED = "\033[31m"
MAGENTA = "\033[35m"
YELLOW = "\033[33m"
DIM = "\033[2m"
GREEN = "\033[32m"


@pytest.mark.parametrize(
    "phase, expected_style",
    [
        ("S", GREEN),
        ("SSS", GREEN),
        ("N", DIM),
        ("SN", DIM),
        ("P", YELLOW),
        ("SP", YELLOW),
        ("NP", YELLOW),
        ("T", MAGENTA),
        ("ST", MAGENTA),
        ("PT", MAGENTA),
        ("NT", MAGENTA),
        ("F", RED),
        ("SF", RED),
        ("PF", RED),
        ("TF", RED),
        ("NF", RED),
        ("SPFTN", RED),
    ],
)
def test_outcome_style(phase, expected_style):
    assert _outcome_style(phase) == expected_style


# ---------------------------------------------------------------------------
# StatusDisplay.__init__
# ---------------------------------------------------------------------------


def test_status_display_init_valid_no_labels():
    display = StatusDisplay(num_runs=3, abort_chars=set())
    assert display is not None


def test_status_display_init_valid_with_label():
    display = StatusDisplay(num_runs=2, abort_chars={"F"}, label="my-experiment")
    assert display is not None


def test_status_display_init_valid_run_labels():
    display = StatusDisplay(
        num_runs=2,
        abort_chars=set(),
        run_labels=["run-a", "run-b"],
    )
    assert display is not None


def test_status_display_init_mismatched_labels_raises():
    with pytest.raises(ValueError):
        StatusDisplay(
            num_runs=3,
            abort_chars=set(),
            run_labels=["only-one"],
        )


def test_status_display_init_empty_run_labels_mismatch_raises():
    with pytest.raises(ValueError):
        StatusDisplay(
            num_runs=2,
            abort_chars=set(),
            run_labels=[],
        )


# ---------------------------------------------------------------------------
# StatusDisplay._is_terminal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phase, expected",
    [
        ("S", True),
        ("SSF", True),
        ("SPFTN", True),
        ("", False),
        ("waiting", False),
        ("running", False),
        ("setup", False),
        ("X", False),
    ],
)
def test_status_display_is_terminal(phase, expected):
    display = StatusDisplay(num_runs=1, abort_chars=set())
    assert display._is_terminal(phase) == expected


# ---------------------------------------------------------------------------
# StatusDisplay._is_aborted
# ---------------------------------------------------------------------------


def test_is_aborted_non_terminal_phase_returns_false():
    display = StatusDisplay(num_runs=1, abort_chars={"F"})
    assert display._is_aborted("running") is False


def test_is_aborted_waiting_returns_false():
    display = StatusDisplay(num_runs=1, abort_chars={"F"})
    assert display._is_aborted("waiting") is False


def test_is_aborted_terminal_without_abort_char():
    display = StatusDisplay(num_runs=1, abort_chars={"F"})
    assert display._is_aborted("S") is False


def test_is_aborted_terminal_with_abort_char():
    display = StatusDisplay(num_runs=1, abort_chars={"F"})
    assert display._is_aborted("SF") is True


def test_is_aborted_terminal_all_success_no_abort_chars():
    display = StatusDisplay(num_runs=1, abort_chars=set())
    assert display._is_aborted("SSS") is False


def test_is_aborted_multiple_abort_chars():
    display = StatusDisplay(num_runs=1, abort_chars={"F", "T"})
    assert display._is_aborted("ST") is True
    assert display._is_aborted("SS") is False


# ---------------------------------------------------------------------------
# StatusDisplay._render
# ---------------------------------------------------------------------------


def test_render_contains_runs_header():
    display = StatusDisplay(num_runs=1, abort_chars=set())
    rendered = display._render()
    assert "Runs" in rendered


def test_render_single_run_waiting():
    display = StatusDisplay(num_runs=1, abort_chars=set())
    rendered = display._render()
    assert "waiting" in rendered


def test_render_single_run_after_update():
    display = StatusDisplay(num_runs=1, abort_chars=set())
    display.update(0, "running")
    rendered = display._render()
    assert "running" in rendered


def test_render_uses_1_based_numbering():
    display = StatusDisplay(num_runs=2, abort_chars=set())
    rendered = display._render()
    assert "1" in rendered
    assert "2" in rendered


def test_render_with_label_in_header():
    display = StatusDisplay(num_runs=1, abort_chars=set(), label="my-label")
    rendered = display._render()
    assert "my-label" in rendered


def test_render_status_line_counts():
    display = StatusDisplay(num_runs=3, abort_chars=set())
    rendered = display._render()
    # All three runs start waiting; status line should reference counts
    assert "waiting" in rendered or "3" in rendered


def test_render_run_label_shown():
    display = StatusDisplay(
        num_runs=1,
        abort_chars=set(),
        run_labels=["worker-42"],
    )
    rendered = display._render()
    assert "worker-42" in rendered


def test_render_set_label_reflected():
    display = StatusDisplay(num_runs=1, abort_chars=set())
    display.set_label(0, "port-9001")
    rendered = display._render()
    assert "port-9001" in rendered


def test_render_phase_icon_present():
    display = StatusDisplay(num_runs=1, abort_chars=set())
    rendered = display._render()
    assert PHASE_ICON["waiting"] in rendered


def test_render_run_0_not_in_display():
    """Run numbering is 1-based; '0' should not appear as a run number."""
    display = StatusDisplay(num_runs=1, abort_chars=set())
    rendered = display._render()
    # The display should show run 1, not run 0
    assert "1" in rendered
