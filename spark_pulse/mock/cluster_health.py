"""Mock cluster health validation for simulation mode.

Mirrors the real cluster_health.py API exactly for testing.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Cluster health check results (mock)."""

    healthy: bool
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @classmethod
    def ok(cls) -> "ValidationResult":
        """Return a healthy result with no warnings or errors."""
        return cls(healthy=True)

    @classmethod
    def with_errors(cls, errors: list[str]) -> "ValidationResult":
        """Return an unhealthy result with the given errors."""
        return cls(healthy=False, errors=errors)

    @classmethod
    def with_warnings(cls, warnings: list[str]) -> "ValidationResult":
        """Return a healthy result with warnings (non-critical)."""
        return cls(healthy=True, warnings=warnings)

    def add_error(self, error: str) -> "ValidationResult":
        """Return a new ValidationResult with an additional error."""
        return ValidationResult(
            healthy=False,
            warnings=self.warnings,
            errors=self.errors + [error],
        )

    def add_warning(self, warning: str) -> "ValidationResult":
        """Return a new ValidationResult with an additional warning."""
        return ValidationResult(
            healthy=self.healthy,
            warnings=self.warnings + [warning],
            errors=self.errors,
        )


class MockClusterHealthValidator:
    """Mock cluster health validator for simulation mode.

    Simulates cluster health checks without real container access.
    """

    def __init__(
        self,
        healthy: bool = True,
        warnings: list[str] | None = None,
        errors: list[str] | None = None,
    ):
        """Initialize mock validator.

        Args:
            healthy: Whether the cluster is healthy.
            warnings: List of warning messages.
            errors: List of error messages.
        """
        self._healthy = healthy
        self._warnings = warnings or []
        self._errors = errors or []
        self._validated_clusters: list[str] = []

    def validate_cluster(
        self,
        cluster_state: object,
        services: object = None,
    ) -> ValidationResult:
        """Validate cluster health (mocked).

        Args:
            cluster_state: ClusterState object (ignored in mock).
            services: Node resolver (ignored in mock).

        Returns:
            ValidationResult with configured health status.
        """
        cluster_name = getattr(cluster_state, "name", "unknown")
        self._validated_clusters.append(cluster_name)

        return ValidationResult(
            healthy=self._healthy,
            warnings=self._warnings,
            errors=self._errors,
        )

    @property
    def validated_clusters(self) -> list[str]:
        """Return list of validated cluster names."""
        return self._validated_clusters.copy()

    def reset(self) -> None:
        """Clear validated clusters history."""
        self._validated_clusters.clear()


def validate_cluster(
    cluster_state: object,
    services: object = None,
) -> ValidationResult:
    """Validate cluster health using default mock validator.

    Args:
        cluster_state: ClusterState object.
        services: Node resolver.

    Returns:
        ValidationResult.
    """
    validator = MockClusterHealthValidator()
    return validator.validate_cluster(cluster_state, services)
