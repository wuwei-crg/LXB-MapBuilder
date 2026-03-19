"""
Auto Map Builder package (active track).

Active:
- NodeMapBuilder (v5)
- NodeExplorer / NavigationMap
- VLM engine and config

Legacy experimental strategies were moved to:
- src/auto_map_builder/legacy/
"""

from .models import ExplorationConfig
from .vlm_engine import VLMEngine, VLMConfig, get_config, set_config
from .node_explorer import (
    NodeExplorer,
    NavigationMap,
    NodeLocator as NodeLocatorV5,
    PageInfo,
    Transition,
    NavNode,
    ExplorationStatus,
)


class NodeMapBuilder:
    """Node-driven map builder (v5, active)."""

    def __init__(self, client, config: ExplorationConfig = None, log_callback=None):
        self.client = client
        self.config = config or ExplorationConfig()
        self.log_callback = log_callback
        self._explorer: NodeExplorer = None
        self._result = None
        self._explore_mode = "serial"
        self._click_delay = 1.5

    @property
    def status(self):
        if self._explorer:
            return self._explorer.status
        return ExplorationStatus.IDLE

    @property
    def nav_map(self) -> NavigationMap:
        if self._explorer:
            return self._explorer.nav_map
        return None

    def set_mode(self, mode: str):
        self._explore_mode = mode
        if self._explorer:
            self._explorer.set_mode(mode)

    def set_click_delay(self, delay: float):
        self._click_delay = delay
        if self._explorer:
            self._explorer.set_click_delay(delay)

    def pause(self):
        if self._explorer:
            self._explorer.pause()

    def resume(self):
        if self._explorer:
            self._explorer.resume()

    def stop(self):
        if self._explorer:
            self._explorer.stop()

    def explore(self, package_name: str):
        self._explorer = NodeExplorer(self.client, self.config, self.log_callback)
        self._explorer.set_mode(self._explore_mode)
        self._explorer.set_click_delay(self._click_delay)
        self._result = self._explorer.explore(package_name)
        return self._result

    def save(self, filepath: str = None):
        if not self._explorer or not self._explorer.nav_map:
            raise RuntimeError("please run explore() first")
        filepath = filepath or f"./maps/{self._result['package']}_nodes.json"
        self._explorer.nav_map.save(filepath)

    def get_realtime_state(self) -> dict:
        if self._explorer:
            return self._explorer.get_realtime_state()
        return {}


__all__ = [
    "ExplorationConfig",
    "ExplorationStatus",
    "NodeMapBuilder",
    "NodeExplorer",
    "NavigationMap",
    "NodeLocatorV5",
    "PageInfo",
    "Transition",
    "NavNode",
    "VLMEngine",
    "VLMConfig",
    "get_config",
    "set_config",
]
