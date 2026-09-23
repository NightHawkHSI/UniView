# UniView - Unity Asset Viewer

A desktop app for browsing the 3D models, textures, sprites and text assets inside Unity games, especially games that have been shut down and can't be played anymore.

## Features

- **Projects page**: a box for each game. Green means its assets are already loaded (it opens instantly); red means they aren't loaded yet. Each box shows how many Unity files and assets the game has.
- **Find Unity games in Steam**: scans your Steam libraries and lets you add games with one click.
- **Model viewer**: rotate and zoom with the mouse wheel. Wireframe, edges and vertex colors can be toggled on and off. The model's texture is found automatically.
- **Model panel**: shows vertex/triangle counts, the source file, what uses the model, and its materials and textures (click one to put it on the model).
- **Texture viewer**: zoom and pan, with a checkerboard background for transparency.
- **Thumbnails** in the asset list.
- **Export**: models as `.obj` + `.mtl` + `.png` (they open textured in Blender), textures as `.png`, and bulk export of everything at once.
- **Console** at the bottom showing what the app is doing, plus `viewer.log` and `crash.log`.

## Run from source

Needs Python 3.10+ on Windows.

```
py -m pip install -r requirements.txt
run.bat
```

Or open a game folder directly: `py unity_viewer.py "D:\SteamLibrary\steamapps\common\Robocraft"`

## Build

```
build.bat
```

This creates:

- `Builds\GitHub`: a clean copy of the source to push to a repository
- `Builds\Release\UniView-vX.Y.Z\`: a standalone app (no Python needed), plus a `.zip` of it for a GitHub release

The packaged exe is automatically self-tested after building. To also test it against a real game:
`build.bat release --test-game "D:\SteamLibrary\steamapps\common\Robocraft"`

The version number is `__version__` at the top of `unity_viewer.py`.

## Notes

- Reads files only; game files are never changed.
- Extracted assets are still owned by the game's creators. Viewing them for personal use is fine, but check before sharing them.
- Built on [UnityPy](https://github.com/K0lb3/UnityPy), [PySide6](https://doc.qt.io/qtforpython-6/) and [PyVista](https://pyvista.org/).
