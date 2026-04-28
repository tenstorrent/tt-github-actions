# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""
Configuration tracking for layer-aware error attribution.

Tracks configurations through the stack to attribute errors to the layer
that set the problematic configuration, not just where the error manifested.
"""

from dataclasses import dataclass, field
from enum import IntEnum


class ConfigLayer(IntEnum):
    """Stack layers ordered from low (hardware) to high (application).

    Higher numbers = higher in the stack (closer to user).
    """

    DRIVER = 1  # Hardware driver, firmware, UMD
    FRAMEWORK = 2  # TT-Metal framework
    OPERATIONS = 3  # TTNN operations
    MODEL = 4  # Model implementations
    SERVING = 5  # vLLM, inference serving
    APPLICATION = 6  # Application layer (run.py, CLI)

    @classmethod
    def from_string(cls, name: str) -> "ConfigLayer":
        """Convert layer name string to ConfigLayer enum."""
        mapping = {
            "driver": cls.DRIVER,
            "framework": cls.FRAMEWORK,
            "operations": cls.OPERATIONS,
            "model": cls.MODEL,
            "serving": cls.SERVING,
            "application": cls.APPLICATION,
        }
        return mapping.get(name.lower(), cls.FRAMEWORK)


@dataclass
class TrackedConfig:
    """A configuration value tracked from the logs."""

    name: str  # Config parameter name (e.g., "max_model_len")
    value: str  # Config value (e.g., "131072")
    layer: ConfigLayer  # Which layer set this config
    source_line: int = 0  # Line number in log where found
    raw_context: str = ""  # Raw line(s) for context


@dataclass
class LayerConfigs:
    """Configurations extracted from each layer."""

    application: dict[str, TrackedConfig] = field(default_factory=dict)
    serving: dict[str, TrackedConfig] = field(default_factory=dict)
    model: dict[str, TrackedConfig] = field(default_factory=dict)
    framework: dict[str, TrackedConfig] = field(default_factory=dict)
    operations: dict[str, TrackedConfig] = field(default_factory=dict)
    driver: dict[str, TrackedConfig] = field(default_factory=dict)

    def get_config(self, name: str) -> TrackedConfig | None:
        """Look up a config by name across all layers (highest layer first)."""
        for layer_configs in [
            self.application,
            self.serving,
            self.model,
            self.operations,
            self.framework,
            self.driver,
        ]:
            if name in layer_configs:
                return layer_configs[name]
        return None

    def all_configs(self) -> list[TrackedConfig]:
        """Return all configs as a flat list."""
        configs = []
        for layer_configs in [
            self.driver,
            self.framework,
            self.operations,
            self.model,
            self.serving,
            self.application,
        ]:
            configs.extend(layer_configs.values())
        return configs

    def get_layer_dict(self, layer: ConfigLayer) -> dict[str, TrackedConfig]:
        """Get the config dict for a specific layer."""
        mapping = {
            ConfigLayer.DRIVER: self.driver,
            ConfigLayer.FRAMEWORK: self.framework,
            ConfigLayer.OPERATIONS: self.operations,
            ConfigLayer.MODEL: self.model,
            ConfigLayer.SERVING: self.serving,
            ConfigLayer.APPLICATION: self.application,
        }
        return mapping.get(layer, {})


@dataclass
class ConfigAttribution:
    """Attribution of an error to a configuration from a higher layer."""

    error_param_name: str  # The parameter mentioned in the error (e.g., "trace_region_size")
    error_layer: ConfigLayer  # Layer where the error occurred (e.g., FRAMEWORK)
    source_config: TrackedConfig | None  # The config that caused the error
    source_layer: ConfigLayer | None  # Layer that set the problematic config
    is_higher_layer_cause: bool = False  # True if source_layer > error_layer
    explanation: str = ""  # Human-readable explanation
    suggested_fix: str = ""  # Actionable fix suggestion

    def __post_init__(self):
        """Compute is_higher_layer_cause after initialization."""
        if self.source_layer is not None and self.error_layer is not None:
            self.is_higher_layer_cause = self.source_layer > self.error_layer
