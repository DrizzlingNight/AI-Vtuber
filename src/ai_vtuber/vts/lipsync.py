from __future__ import annotations

from ai_vtuber.config import ActionsConfig, ParameterAction
from ai_vtuber.vts.actions import ActionMappingError
from ai_vtuber.vts.inventory import InputParameterResource, VTSService


class ConfiguredMouthSink:
    def __init__(
        self,
        service: VTSService,
        config: ActionsConfig,
        *,
        semantic_name: str,
    ) -> None:
        self.service = service
        self.config = config
        self.semantic_name = semantic_name
        self._binding: ParameterAction | None = None
        self._resource: InputParameterResource | None = None

    async def prepare(self) -> None:
        binding = self.config.actions.get(self.semantic_name)
        if not isinstance(binding, ParameterAction):
            raise ActionMappingError(
                f"Mouth action {self.semantic_name!r} is not a parameter mapping"
            )
        inventory = await self.service.ensure_inventory_current()
        if inventory.model.model_id != self.config.model_id:
            raise ActionMappingError(
                f"Mouth mapping is for {self.config.model_name!r}, but current model "
                f"is {inventory.model.name!r}; run inventory again"
            )
        resource = inventory.find_input_parameter(binding.target)
        if resource is None:
            raise ActionMappingError(
                f"Mouth parameter target {binding.target!r} is missing"
            )
        for label, value in (
            ("peak_value", binding.peak_value),
            ("neutral_value", binding.neutral_value),
        ):
            if not resource.minimum <= value <= resource.maximum:
                raise ActionMappingError(
                    f"{self.semantic_name}.{label} {value} is outside "
                    f"{resource.name} range [{resource.minimum}, {resource.maximum}]"
                )
        self._binding = binding
        self._resource = resource

    async def set_level(self, level: float) -> None:
        if not 0 <= level <= 1:
            raise ValueError("Mouth level must be between zero and one")
        binding, resource = self._prepared()
        value = binding.neutral_value + (
            binding.peak_value - binding.neutral_value
        ) * level
        await self.service.inject_parameter(
            resource.name,
            value,
            weight=binding.weight,
        )

    async def reset(self) -> None:
        if self._binding is None or self._resource is None:
            return
        await self.service.inject_parameter(
            self._resource.name,
            self._binding.neutral_value,
            weight=1.0,
        )

    def _prepared(self) -> tuple[ParameterAction, InputParameterResource]:
        if self._binding is None or self._resource is None:
            raise RuntimeError("Mouth sink must be prepared before playback")
        return self._binding, self._resource
