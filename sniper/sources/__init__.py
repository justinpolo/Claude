from .base import BlockedError, Source, SourceError
from .depop import DepopSource
from .vinted import VintedSource

__all__ = ["Source", "SourceError", "BlockedError", "VintedSource", "DepopSource", "build_sources"]


def build_sources(cfg: dict, client=None) -> dict:
    out = {}
    pcfg = cfg.get("platforms", {})
    if pcfg.get("vinted", {}).get("enabled", True):
        out["vinted"] = VintedSource(pcfg.get("vinted", {}), client=client)
    if pcfg.get("depop", {}).get("enabled", False):
        out["depop"] = DepopSource(pcfg.get("depop", {}), client=client)
    return out
