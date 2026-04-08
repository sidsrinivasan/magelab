import logging

import pytest

from magelab.orchestrator import RunOutcome
from magelab.pipeline.execution import _outcome_string, _setup_logging


@pytest.fixture
def logs_dir(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# _outcome_string tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcomes, expected",
    [
        ([], ""),
        ([RunOutcome.SUCCESS], "S"),
        ([RunOutcome.FAILURE], "F"),
        ([RunOutcome.SUCCESS, RunOutcome.SUCCESS, RunOutcome.FAILURE], "SSF"),
        ([RunOutcome.SUCCESS, RunOutcome.PARTIAL, RunOutcome.TIMEOUT], "SPT"),
        ([RunOutcome.NO_WORK], "N"),
        (
            [
                RunOutcome.NO_WORK,
                RunOutcome.SUCCESS,
                RunOutcome.PARTIAL,
                RunOutcome.FAILURE,
                RunOutcome.TIMEOUT,
            ],
            "NSPFT",
        ),
    ],
)
def test_outcome_string(outcomes, expected):
    assert _outcome_string(outcomes) == expected


# ---------------------------------------------------------------------------
# _setup_logging tests
# ---------------------------------------------------------------------------


def test_setup_logging_returns_logger(logs_dir):
    logger = _setup_logging(logs_dir / "logs")
    assert isinstance(logger, logging.Logger)


def test_setup_logging_logger_name_contains_dir_name(logs_dir):
    logger = _setup_logging(logs_dir / "logs")
    assert logs_dir.name in logger.name


def test_setup_logging_logger_level_is_info(logs_dir):
    logger = _setup_logging(logs_dir / "logs")
    assert logger.level == logging.INFO


def test_setup_logging_propagate_is_false(logs_dir):
    logger = _setup_logging(logs_dir / "logs")
    assert logger.propagate is False


def test_setup_logging_has_exactly_one_file_handler(logs_dir):
    logger = _setup_logging(logs_dir / "logs")
    file_handlers = [h for h in logger.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1


def test_setup_logging_file_handler_points_to_framework_log(logs_dir):
    logger = _setup_logging(logs_dir / "logs")
    file_handlers = [h for h in logger.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1
    expected = str(logs_dir / "logs" / "framework.log")
    assert file_handlers[0].baseFilename == expected


def test_setup_logging_no_duplicate_handlers_on_second_call(logs_dir):
    _setup_logging(logs_dir / "logs")
    logger = _setup_logging(logs_dir / "logs")
    file_handlers = [h for h in logger.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1
