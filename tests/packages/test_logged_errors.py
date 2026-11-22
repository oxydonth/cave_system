"""The check that a test of the package logs no error it does not provoke (``no_logged_errors``).

It used to read ``caplog.records`` after the test, which in a fixture's teardown is the records of
that teardown only, so it saw no error of the test at all; and it allowed every error whose text
contained "send a notification copy" in every test.
"""

import logging

import pytest

from tests.packages.helpers import failed_copy, logged_errors, unexpected_errors

LOG = logging.getLogger("tests.packages.test_logged_errors")
MISSING = "Action notify.does_not_exist not found"


def _record(message: str, level: int = logging.ERROR) -> logging.LogRecord:
    return logging.LogRecord("x", level, __file__, 1, message, None, None)


@pytest.fixture
def logs_while_set_up(expected_errors: list[str]) -> None:
    """A fixture that logs an error while it is set up, as a failing package set-up would."""
    expected_errors.append("an error while setting up")
    LOG.error("an error while setting up")


def test_errors_of_the_set_up_and_of_the_test_are_both_seen(
    caplog: pytest.LogCaptureFixture, logs_while_set_up: None, expected_errors: list[str]
) -> None:
    expected_errors.append("an error in the test")
    LOG.error("an error in the test")
    LOG.warning("a warning is not an error")

    seen = [record.getMessage() for record in logged_errors(caplog)]

    assert seen == ["an error while setting up", "an error in the test"]


def test_an_error_nobody_expects_is_reported() -> None:
    records = [_record("Template error: something broke")]

    assert unexpected_errors(records, []) == ["Template error: something broke"]


def test_a_failed_copy_is_not_allowed_in_a_test_that_does_not_expect_it() -> None:
    """The old check let every error mentioning the copy automation through, in every test."""
    message = (
        "Cave: send a notification copy: Error executing script. Service not found for if at "
        f"pos 2: {MISSING}"
    )

    assert unexpected_errors([_record(message)], []) == [message]
    assert unexpected_errors([_record(message)], failed_copy(MISSING)) == []


@pytest.mark.parametrize(
    "message",
    [
        # Another reason than the one the test provokes.
        "Cave: send a notification copy: Error executing script. Service not found for if at "
        "pos 2: Action notify.other_phone not found",
        # The expected text with something after it.
        "Cave: send a notification copy: Error executing script. Service not found for if at "
        f"pos 2: {MISSING}; and a template error",
        # Another error of the same automation.
        "Cave: send a notification copy: Error rendering variables: UndefinedError",
    ],
    ids=["other_reason", "more_text", "other_error"],
)
def test_only_the_expected_text_as_a_whole_is_allowed(message: str) -> None:
    assert unexpected_errors([_record(message)], failed_copy(MISSING)) == [message]


def test_a_warning_is_not_an_error() -> None:
    assert unexpected_errors([_record("only a warning", logging.WARNING)], []) == []
