"""Domain adapter for curated Ascend NPU fault-diagnosis cases."""

from __future__ import annotations

from typing import Any, Mapping

from data_designer.pipelines.envsynth.domain_adapters.base import (
    register_domain_adapter,
)
from data_designer.pipelines.envsynth.domain_adapters.builtin import (
    TerminalDomainAdapter,
)
from data_designer.pipelines.envsynth.domains import ascend_fault_profile as profile


class AscendFaultDiagnosisDomainAdapter(TerminalDomainAdapter):
    """Terminal-profile adapter for the Ascend fault-diagnosis family."""

    family = profile.FAMILY

    def supports(self, domain: Mapping[str, Any], draft: Mapping[str, Any]) -> bool:
        del draft
        return (
            domain.get("family") == self.family
            or domain.get("schemaVersion") == profile.DOMAIN_SCHEMA_VERSION
        )


register_domain_adapter(AscendFaultDiagnosisDomainAdapter(), replace=True)


__all__ = ["AscendFaultDiagnosisDomainAdapter"]
