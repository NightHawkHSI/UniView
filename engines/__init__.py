"""Engine plugins: the built-in ones in this package (Unity, loose files) plus plugins/*.py (Source, Unreal, ...)."""

import importlib
import importlib.util
import logging
import os
import sys
import traceback
from typing import NamedTuple

from .sdk import API_VERSION, EnginePlugin

log = logging.getLogger("viewer.engines")

BUILTIN_MODULES = ("unity", "generic")  # Source, Source 2 and Unreal ship as plugins/*.py



class LoadEntry(NamedTuple):
    """One line of the plugin manager: a plugin that loaded, failed or was switched off."""
    name: str
    origin: str      # "built-in" or the plugins/ file name
    ok: bool
    message: str
    key: str         # what the user's disabled list holds for it: plugin id (built-in) or file name (user)
    disabled: bool = False
    plugin: EnginePlugin | None = None


_plugins = []        # [EnginePlugin] in load order (built-ins first)
_load_report = []    # [LoadEntry] for Options -> Plugin manager


def _register(plugin, origin, key):
    if not isinstance(plugin, EnginePlugin):
        raise TypeError("PLUGIN must be an EnginePlugin instance")
    if getattr(plugin, "api_version", API_VERSION) > API_VERSION:
        raise RuntimeError(f"needs plugin API v{plugin.api_version}, this UniView has v{API_VERSION}")
    old = next((p for p in _plugins if p.id == plugin.id), None)
    if old is not None:
        _plugins.remove(old)  # a user plugin with the same id replaces the built-in one
        log.info("Plugin '%s' from %s replaces the built-in one", plugin.id, origin)
    _plugins.append(plugin)
    _load_report.append(LoadEntry(plugin.name, origin, True, f"v{plugin.version}", key, plugin=plugin))


def _plugins_from_module(module):
    found = getattr(module, "PLUGINS", None) or [getattr(module, "PLUGIN", None)]
    return [p for p in found if p is not None]


def load_plugins(user_dir=None, disabled=()):
    """Load built-in engines, then every plugins/*.py (not starting with '_'). Safe to call once.

    disabled: plugin ids (built-in engines) and file names (plugins/ files) the user switched off.
    """
    if _plugins:
        return list(_plugins)
    disabled = set(disabled)
    for name in BUILTIN_MODULES:
        try:
            module = importlib.import_module(f"{__name__}.{name}")
            for plugin in _plugins_from_module(module):
                if plugin.id in disabled:
                    _load_report.append(LoadEntry(plugin.name, "built-in", True, "switched off", plugin.id,
                                                  disabled=True))
                    log.info("Engine '%s' is switched off (plugin manager)", plugin.id)
                    continue
                _register(plugin, "built-in", plugin.id)
        except Exception as e:
            log.error("Built-in engine '%s' failed to load: %s\n%s", name, e, traceback.format_exc().rstrip())
            _load_report.append(LoadEntry(name, "built-in", False, f"{type(e).__name__}: {e}", name))
    if user_dir and os.path.isdir(user_dir):
        if user_dir not in sys.path:
            sys.path.insert(0, user_dir)  # lets a plugin split itself into helper modules
        for fname in sorted(os.listdir(user_dir)):
            if not fname.endswith(".py") or fname.startswith("_"):
                continue
            if fname in disabled:
                _load_report.append(LoadEntry(fname, fname, True, "switched off", fname, disabled=True))
                log.info("Plugin %s is switched off (plugin manager)", fname)
                continue
            path = os.path.join(user_dir, fname)
            mod_name = f"uniview_plugin_{os.path.splitext(fname)[0]}"
            try:
                spec = importlib.util.spec_from_file_location(mod_name, path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = module
                spec.loader.exec_module(module)
                plugins = _plugins_from_module(module)
                if not plugins:
                    raise RuntimeError("no PLUGIN = MyEnginePlugin() found in the file")
                for plugin in plugins:
                    _register(plugin, fname, fname)
                log.info("Loaded engine plugin %s", fname)
            except KeyboardInterrupt:
                raise
            except BaseException as e:  # also SystemExit / pytest.skip(): a bad plugin must never stop UniView starting
                sys.modules.pop(mod_name, None)
                log.error("Plugin %s failed to load: %s\n%s", fname, e, traceback.format_exc().rstrip())
                _load_report.append(LoadEntry(fname, fname, False, f"{type(e).__name__}: {e}", fname))
    return list(_plugins)


def plugins():
    return list(_plugins)


def load_report():
    return list(_load_report)


def get(engine_id):
    return next((p for p in _plugins if p.id == engine_id), None)


def detect(path):
    """(plugin, score) of the engine that best matches a game folder, or (None, 0)."""
    best, best_score = None, 0
    for plugin in _plugins:
        try:
            score = plugin.detect(path) or 0
        except Exception as e:
            log.debug("%s.detect(%s) failed: %s", plugin.id, path, e)
            score = 0
        if score > best_score:
            best, best_score = plugin, score
    return best, best_score
