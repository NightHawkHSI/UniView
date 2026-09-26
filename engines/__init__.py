"""Engine plugins: the built-in ones in this package plus user plugins from the plugins/ folder."""

import importlib
import importlib.util
import logging
import os
import sys
import traceback

from .sdk import API_VERSION, EnginePlugin

log = logging.getLogger("viewer.engines")

BUILTIN_MODULES = ("unity", "source", "source2", "unreal", "fallout", "generic")

_plugins = []        # [EnginePlugin] in load order (built-ins first)
_load_report = []    # [(name, origin, ok, message)] for Help -> Engine plugins


def _register(plugin, origin):
    if not isinstance(plugin, EnginePlugin):
        raise TypeError("PLUGIN must be an EnginePlugin instance")
    if getattr(plugin, "api_version", API_VERSION) > API_VERSION:
        raise RuntimeError(f"needs plugin API v{plugin.api_version}, this UniView has v{API_VERSION}")
    old = next((p for p in _plugins if p.id == plugin.id), None)
    if old is not None:
        _plugins.remove(old)  # a user plugin with the same id replaces the built-in one
        log.info("Plugin '%s' from %s replaces the built-in one", plugin.id, origin)
    _plugins.append(plugin)
    _load_report.append((plugin.name, origin, True, f"v{plugin.version}"))


def _plugins_from_module(module):
    found = getattr(module, "PLUGINS", None) or [getattr(module, "PLUGIN", None)]
    return [p for p in found if p is not None]


def load_plugins(user_dir=None):
    """Load built-in engines, then every plugins/*.py (not starting with '_'). Safe to call once."""
    if _plugins:
        return list(_plugins)
    for name in BUILTIN_MODULES:
        try:
            module = importlib.import_module(f"{__name__}.{name}")
            for plugin in _plugins_from_module(module):
                _register(plugin, "built-in")
        except Exception as e:
            log.error("Built-in engine '%s' failed to load: %s\n%s", name, e, traceback.format_exc().rstrip())
            _load_report.append((name, "built-in", False, f"{type(e).__name__}: {e}"))
    if user_dir and os.path.isdir(user_dir):
        if user_dir not in sys.path:
            sys.path.insert(0, user_dir)  # lets a plugin split itself into helper modules
        for fname in sorted(os.listdir(user_dir)):
            if not fname.endswith(".py") or fname.startswith("_"):
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
                    _register(plugin, fname)
                log.info("Loaded engine plugin %s", fname)
            except Exception as e:
                log.error("Plugin %s failed to load: %s\n%s", fname, e, traceback.format_exc().rstrip())
                _load_report.append((fname, fname, False, f"{type(e).__name__}: {e}"))
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
