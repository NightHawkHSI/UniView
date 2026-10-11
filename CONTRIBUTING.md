# Contributing to UniView

Thanks for helping. Bug reports, game compatibility reports, new engine plugins and fixes are all welcome.

## Reporting a bug

Open an [issue](https://github.com/NightHawkHSI/UniView/issues) and include:

- The UniView version (the release you downloaded, or the first line of `viewer.log`) and the game (name,
  store, engine if you know it).
- What you did, what you expected and what happened instead.
- The end of `viewer.log` (**Help → Open log file**; it sits next to `UniView.exe`, or next to
  `unity_viewer.py` when running from source). Errors and tracebacks land there.

Please don't attach game files. UniView reads games you own; their files belong to their makers.

## Adding support for a new engine

You don't need to change UniView itself: an engine is a single `.py` file in `plugins/`.

1. Copy [`plugins/_template.py`](plugins/_template.py) to `plugins/my_engine.py`. The template is a working
   "loose files" plugin (images, text, `.obj` models), so you start from something that runs.
2. Read [PLUGINS.md](PLUGINS.md) for the API: assets, mesh conventions, materials, progress reporting and the
   helpers for Valve VPK and Unreal pak archives.
3. For a full real-world example see [`plugins/terraria.py`](plugins/terraria.py) (XNA/FNA: compressed `.xnb`
   textures, sounds, wave banks, names read from the game's own exe).

A plugin can be shared as just that file. If you want it to ship with UniView, open a pull request that adds it
to `plugins/` with a test in `tests/` (tests that need the real game should skip when it isn't installed; see
`tests/test_xnb.py`).

## Working on UniView itself

Needs Python 3.10+ on Windows.

```
py -m pip install -r requirements-dev.txt
py unity_viewer.py
```

Layout:

| Folder | What's in it |
|---|---|
| `unity_viewer.py` | Entry point |
| `uniview/` | The app: windows, viewer, export, Unity project export, Mod Maker |
| `uniview/ui/` | Qt widgets and dialogs |
| `engines/` | Built-in engines (Unity, loose files) and shared format readers (VPK, BSP, KV3, Unreal pak/IoStore/packages); `engines/sdk.py` is the plugin API |
| `plugins/` | Engine plugins loaded at startup (Source, Source 2, Unreal, Spore and the other game plugins, `_template.py`); Options → Plugin manager switches them off / installs / removes them |
| `tests/` | pytest tests |

Before sending a pull request, run the same checks the build runs:

```
py -m ruff check .
py -m mypy --ignore-missing-imports --follow-imports=silent --check-untyped-defs engines/sdk.py plugins
py -m pytest tests -q
```

`build.bat` (or `py build.py`) runs all three, then builds the standalone app; see the README's
[Build](README.md#build) section.

Style:

- Match the code around you: plain names, short comments where the *why* isn't obvious, lines up to 120
  characters.
- Keep the UI responsive: slow work (reading archives, decoding, exporting) runs off the UI thread.
- New dependencies need a good reason, a license compatible with the GPL, and an entry in
  [CREDITS.md](CREDITS.md).

## License

UniView is under the [GNU General Public License v3.0](LICENSE). By contributing you agree that your
contribution is released under the same license.
