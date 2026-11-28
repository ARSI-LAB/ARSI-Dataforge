"""Shared data foundation for the DataForge packages."""

from typing import Any

__all__ = [
    "DataValidationConfig",
    "MultimodalDataset",
    "ValidationResult",
]

_SUBMODULES = ("dataset", "embodied", "provenance", "trajectories")


def __getattr__(name: str) -> Any:
    """Resolve the re-exported dataset symbols on first use.

    ``base.dataset`` imports Ray, so the package initializer stays lazy: a
    lightweight contract module such as ``base.embodied`` can be imported
    without pulling the Ray runtime in.
    """
    if name in __all__:
        from .dataset import DataValidationConfig, MultimodalDataset, ValidationResult

        return {
            "DataValidationConfig": DataValidationConfig,
            "MultimodalDataset": MultimodalDataset,
            "ValidationResult": ValidationResult,
        }[name]
    if name in _SUBMODULES:
        import importlib

        return importlib.import_module(f".{name}", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
