"""agentbench: run one task with and without a tool, report the difference."""

__version__ = "0.1.1"
__all__ = ["__version__", "BenchError"]


class BenchError(Exception):
    """A loud, structured failure.

    ``code`` is a short machine-readable code (for example ``TASK_INVALID``).
    ``blocked`` says what stopped the work, ``fix`` says what to do about
    it, and ``if_that_fails`` says the next step if the fix does not work.
    ``exit_code`` follows the plugin contract: 2 for usage / task errors,
    3 for tool failures.
    """

    def __init__(self, code, blocked, fix, if_that_fails, exit_code=2):
        super().__init__(blocked)
        self.code = code
        self.blocked = blocked
        self.fix = fix
        self.if_that_fails = if_that_fails
        self.exit_code = exit_code

    def as_dict(self):
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "blocked": self.blocked,
                "fix": self.fix,
                "if_that_fails": self.if_that_fails,
            },
        }
