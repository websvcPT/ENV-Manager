"""Exception types shared across the application."""


class ExpectedOutcome(Exception):
    """Base class for normal results that surface as exceptions.

    Blocked or halted deploys, failed logins and non-zero post-deploy exits are expected
    outcomes, so error reporting ignores them.
    """
