"""Resolve the cluster reader an investigation should use from its session state."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from triage import state_keys
from triage.config import ClusterMode, get_settings
from triage.k8s.base import ClusterReader
from triage.k8s.replay import ReplayClusterClient

ReaderFactory = Callable[[str, str | None], ClusterReader]


def _default_factory(mode: str, scenario: str | None) -> ClusterReader:
    settings = get_settings()
    if mode == ClusterMode.LIVE:
        from triage.k8s.live import LiveClusterClient

        return LiveClusterClient(context=settings.kube_context)
    return ReplayClusterClient.from_scenario(
        settings.replay_fixtures_dir, scenario or settings.default_scenario
    )


class ReaderRegistry:
    """Caches one reader per (mode, scenario) and lets tests inject fakes."""

    def __init__(self, factory: ReaderFactory = _default_factory) -> None:
        """Use ``factory`` to build readers on first use."""
        self._factory = factory
        self._cache: dict[tuple[str, str | None], ClusterReader] = {}

    def set_factory(self, factory: ReaderFactory) -> None:
        """Replace the factory (and drop cached readers). Used by tests and the eval."""
        self._factory = factory
        self._cache.clear()

    def for_state(self, state: Any) -> ClusterReader:
        """Return the reader for a session state mapping."""
        mode = str(state.get(state_keys.CLUSTER_MODE) or get_settings().cluster_mode)
        scenario = state.get(state_keys.SCENARIO)
        key = (mode, scenario)
        if key not in self._cache:
            self._cache[key] = self._factory(mode, scenario)
        return self._cache[key]


registry = ReaderRegistry()
