"""Temporary fixture to exercise the Telegram PR-review approval flow.

Not imported anywhere and safe to delete — this exists only so a pull request
contains a change with an obvious, flaggable issue, so the AI review produces a
finding (rather than being suppressed) and a Telegram approval prompt is sent.
"""


def average(values):
    # No guard for an empty list: sum([]) / len([]) raises ZeroDivisionError.
    return sum(values) / len(values)


def first_even(numbers):
    # Returns None implicitly when there is no even number; callers that expect
    # an int will break.
    for n in numbers:
        if n % 2 == 0:
            return n
