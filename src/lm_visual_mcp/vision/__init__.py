"""Vision module: image-recognition capability.

- ``types``: provider-neutral domain model (image only, no video).
- ``providers``: concrete providers behind a type registry, each owning its
  own rate limiter (rpm / concurrency).
- ``router``: ordered chain with fallback - provider-neutral.
- ``service``: the single entry point owning the chain and concurrency gate.

Heavy members (router/service) are exported lazily to keep the import graph
acyclic: ``errors`` imports ``vision.types`` at module load, and the service in
turn imports the top-level config/errors.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types import ImageInput, ImageRequest, ProviderResult, ProviderStatus

if TYPE_CHECKING:  # pragma: no cover - import-time cycle avoidance
    from .router import RoutedResult, VisionRouter
    from .service import VisionService

__all__ = [
    "VisionService",
    "VisionRouter",
    "RoutedResult",
    "ImageInput",
    "ImageRequest",
    "ProviderResult",
    "ProviderStatus",
]


def __getattr__(name: str):
    if name == "VisionService":
        from .service import VisionService

        return VisionService
    if name == "VisionRouter":
        from .router import VisionRouter

        return VisionRouter
    if name == "RoutedResult":
        from .router import RoutedResult

        return RoutedResult
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
