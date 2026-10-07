"""In-process counters and timings for the request path."""

from app.config import settings


class MetricsCollector:
    """Records counters and recent observations when metrics are enabled."""

    def __init__(self) -> None:
        self._counters: dict[tuple, int] = {}
        self._observations: dict[tuple, list[float]] = {}

    def _enabled(self) -> bool:
        return settings.metrics_enabled

    def increment(self, name: str, value: int = 1, **labels: str) -> None:
        """Add to a counter. No-ops when metrics_enabled is false."""
        if not self._enabled():
            return
        key = (name, tuple(sorted(labels.items())))
        self._counters[key] = self._counters.get(key, 0) + value

    def observe(self, name: str, value: float, **labels: str) -> None:
        """Record a timing sample. Keeps the latest 200 samples per series."""
        if not self._enabled():
            return
        key = (name, tuple(sorted(labels.items())))
        samples = self._observations.setdefault(key, [])
        samples.append(float(value))
        if len(samples) > 200:
            del samples[:-200]

    def snapshot(self) -> dict:
        """Return a JSON-friendly view of every series."""
        counters = [
            {"name": name, "labels": dict(labels), "value": value}
            for (name, labels), value in self._counters.items()
        ]
        observations = []
        for (name, labels), samples in self._observations.items():
            average = sum(samples) / len(samples) if samples else 0
            observations.append(
                {
                    "name": name,
                    "labels": dict(labels),
                    "count": len(samples),
                    "avg": average,
                }
            )
        return {
            "enabled": self._enabled(),
            "counters": counters,
            "observations": observations,
        }


metrics = MetricsCollector()
