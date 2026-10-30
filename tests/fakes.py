"""Stand-ins shared by the tests."""


class FakeHealth:
    """Records the signals pollers and monitor.forward_new send to src/health.py."""

    def __init__(self):
        self.sweeps, self.forwards, self.forward_failures = [], 0, 0

    def register(self, feed):
        pass

    def sweep(self, feed, ok, total):
        self.sweeps.append((feed, ok, total))

    def forwarded(self):
        self.forwards += 1

    def forward_failed(self):
        self.forward_failures += 1
