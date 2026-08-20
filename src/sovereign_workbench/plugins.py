from __future__ import annotations

from dataclasses import asdict
from pathlib import Path


class PluginRuntimeError(RuntimeError):
    pass


def _registry():
    try:
        from sovereign_plugins import default_registry
    except ImportError as exc:
        raise PluginRuntimeError(
            "Plugin pack is not installed; install the Workbench 'plugins' extra"
        ) from exc
    return default_registry()


def list_plugins() -> list[dict]:
    return [asdict(manifest) for manifest in _registry().manifests()]


def run_plugin(plugin_id: str, path: Path, *, max_bytes: int = 100 * 1024 * 1024) -> dict:
    from sovereign_plugins.contracts import PluginRequest, hash_file

    plugin = _registry().get(plugin_id)
    resolved = path.resolve(strict=True)
    if resolved.suffix.casefold() not in plugin.manifest.accepted_suffixes:
        raise PluginRuntimeError("Plugin does not accept this file type")
    digest = hash_file(resolved, max_bytes)
    result = plugin.run(PluginRequest(resolved, digest))
    value = result.to_dict()
    if (
        value.get("contract_version") != "sovereign.plugin.result.v1"
        or value.get("authority") != "none"
        or value.get("status") != "candidate"
        or value.get("input_sha256") != digest
    ):
        raise PluginRuntimeError("Plugin result failed identity or authority binding")
    return value
