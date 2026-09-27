"""--self-test: checks imports, plugins, rendering and optionally loading a real game."""

import pyvista as pv

import engines
from uniview.constants import log
from uniview.ui.main_window import MainWindow
from uniview.workers import Loader, meshdata_to_polydata


def self_test(game_path=None):
    """Check the libraries work (used by build.py on the packaged exe). Returns an exit code."""
    failures = []

    def check(name, fn):
        try:
            detail = fn()
            log.info("self-test OK    %s%s", name, f" ({detail})" if detail else "")
        except Exception as e:
            failures.append(name)
            log.error("self-test FAIL  %s: %s: %s", name, type(e).__name__, e)

    def imports():
        import importlib
        mods = ("UnityPy", "texture2ddecoder", "etcpak", "astc_encoder", "lz4.block", "brotli")
        for mod in mods:
            importlib.import_module(mod)
        return ", ".join(mods)

    def plugins():
        failed = [f"{name}: {msg}" for name, _origin, ok, msg in engines.load_report() if not ok]
        if failed:
            raise RuntimeError("; ".join(failed))
        return ", ".join(p.id for p in engines.plugins())

    def render():
        plotter = pv.Plotter(off_screen=True, window_size=(64, 64))
        plotter.add_mesh(pv.Sphere())
        shape = plotter.screenshot(return_img=True).shape
        plotter.close()
        return f"offscreen image {shape}"

    def window():
        MainWindow(None).close()

    def game():
        plugin, _score = engines.detect(game_path)
        if plugin is None:
            raise RuntimeError("no engine plugin recognizes this folder")
        result = {}
        loader = Loader(game_path, plugin)
        loader.finished.connect(lambda session: result.update(session=session))
        loader.run()
        if "session" not in result:
            raise RuntimeError("loading failed (see log above)")
        session = result["session"]
        counts = {}
        for kind, decode in (("model", lambda a: meshdata_to_polydata(session.mesh(a))),
                             ("texture", lambda a: session.image(a).load())):
            ok = 0
            items = [a for a in session.assets if a.kind == kind][:25]
            for asset in items:
                try:
                    with session.lock:
                        decode(asset)
                    ok += 1
                except Exception as e:
                    log.warning("self-test: could not decode %s '%s': %s", kind, asset.name, e)
            if items and not ok:
                raise RuntimeError(f"none of the first {len(items)} {kind} assets decoded")
            counts[kind] = f"{ok}/{len(items)}"
        session.close()
        return (f"{plugin.name}: {session.file_count} files, decoded models {counts.get('model')}, "
                f"textures {counts.get('texture')}")

    check("imports", imports)
    check("engine plugins", plugins)
    check("3D rendering", render)
    check("main window", window)
    if game_path:
        check(f"load {game_path}", game)
    log.info("self-test %s", "PASSED" if not failures else f"FAILED: {', '.join(failures)}")
    return 1 if failures else 0
