<div align="center">

# UniView - Unity Asset Viewer

**Browse the 3D models, textures, sprites and text assets inside Unity games, including games that have shut down and can't be played anymore.**

[![Views](https://hits.sh/github.com/NightHawkHSI/UniView.svg?label=views&color=4c1)](https://hits.sh/github.com/NightHawkHSI/UniView/)
[![Downloads](https://img.shields.io/github/downloads/NightHawkHSI/UniView/total?label=downloads&color=blue)](https://github.com/NightHawkHSI/UniView/releases)
[![Latest release](https://img.shields.io/github/v/release/NightHawkHSI/UniView?label=version)](https://github.com/NightHawkHSI/UniView/releases/latest)
[![Release date](https://img.shields.io/github/release-date/NightHawkHSI/UniView?label=released)](https://github.com/NightHawkHSI/UniView/releases/latest)
<br>
[![Platform](https://img.shields.io/badge/platform-Windows%20x64-0078D6?logo=windows)](https://github.com/NightHawkHSI/UniView/releases/latest)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](#run-from-source)
[![Stars](https://img.shields.io/github/stars/NightHawkHSI/UniView?style=flat&color=yellow)](https://github.com/NightHawkHSI/UniView/stargazers)
[![Last commit](https://img.shields.io/github/last-commit/NightHawkHSI/UniView)](https://github.com/NightHawkHSI/UniView/commits/main)
[![Repo size](https://img.shields.io/github/repo-size/NightHawkHSI/UniView)](https://github.com/NightHawkHSI/UniView)

### [![Download UniView](https://img.shields.io/badge/%E2%AC%87%20Download-UniView%20for%20Windows-2ea44f?style=for-the-badge&logo=windows)](https://github.com/NightHawkHSI/UniView/releases/latest)

Unzip it and run `UniView.exe`. You don't need Python.

[Download](#download) · [Features](#features) · [How to use](#how-to-use) · [Run from source](#run-from-source) · [Build](#build) · [FAQ](#faq)

<img src="Icon.png" alt="UniView projects page" width="850">

</div>

---

## At a glance

| | |
|---|---|
| **What it opens** | Any Unity game folder: `.assets`, `level` files, AssetBundles, `.resS` streams |
| **What it shows** | Meshes (3D), Texture2D, Sprites, TextAssets |
| **What it exports** | Models as `.obj` + `.mtl` + `.png` (open textured in Blender), textures as `.png`, text as `.txt` |
| **Game files** | Read only. UniView never changes anything |
| **Tested with** | Robocraft, Muck, Valheim, Trailmakers and other Unity games on Steam |
| **Built with** | [UnityPy](https://github.com/K0lb3/UnityPy) · [PySide6](https://doc.qt.io/qtforpython-6/) · [PyVista](https://pyvista.org/) |

## Download

1. Go to the [**latest release**](https://github.com/NightHawkHSI/UniView/releases/latest).
2. Download `UniView-vX.Y.Z-win64.zip`.
3. Unzip it anywhere and run **`UniView.exe`**.

> Windows SmartScreen may warn you the first time because the exe isn't code-signed. Click **More info → Run anyway**.

## Features

- **Projects page**: a box for each game. **Green** means its assets are already loaded (it opens instantly); **red** means they aren't loaded yet. Each box shows how many Unity files and assets the game has.
- **Find Unity games in Steam**: scans all your Steam libraries and lets you add games with one click.
- **Model viewer**: rotate, and zoom with the mouse wheel or +/−. Wireframe, edges, vertex colors and texture alpha can each be toggled on and off. The model's texture is found automatically.
- **Model panel**: shows vertex/triangle counts, the source file, the prefab path, what uses the model, and its materials and textures. Click a texture to put it on the model.
- **Texture viewer**: zoom under the mouse, drag to pan, with a checkerboard background for transparency.
- **Thumbnails** of models and textures in the asset list.
- **Export**: one asset at a time, a multi-selection, or all models/textures at once.
- **Console** at the bottom showing what the app is doing, plus `viewer.log` and `crash.log`.

## How to use

1. Start UniView. The **Projects** page opens.
2. Click **Find Unity games in Steam**, or **＋ Add game** and pick a game's install folder (e.g. `...\steamapps\common\Robocraft`).
3. Click a game's box to load it.
4. Pick an asset in the list on the left:
   - **Model**: rotate it in 3D and check its textures in the right-hand panel. **Save model + textures** exports it.
   - **Texture**: zoom and pan it. **Save PNG** exports it.
5. Right-click assets for more options. Use **File → Export all …** for bulk export.
6. **← Projects** takes you back. Right-click a game's box to rename it, unload it from memory, or remove it.

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

- `Builds\GitHub`: a clean copy of the source (this repo)
- `Builds\Release\UniView-vX.Y.Z\`: a standalone app (no Python needed), plus a `.zip` of it for a GitHub release

The packaged exe is automatically self-tested after building. To also test it against a real game:
`build.bat release --test-game "D:\SteamLibrary\steamapps\common\Robocraft"`

The version number is `__version__` at the top of `unity_viewer.py`.

## FAQ

**A model shows no texture.**
Not every model can be traced back to a material. Right-click any texture in the list and choose **Apply as texture to current model**.

**A model looks see-through or odd.**
Leave **Texture alpha** off. Many games store other data in the alpha channel. If the texture is upside down, try **Flip texture V**.

**Something crashed or didn't load.**
Check the **Console**, or open `crash.log` / `viewer.log` next to `UniView.exe`. Please attach them when you [open an issue](https://github.com/NightHawkHSI/UniView/issues).

**Does it use a lot of memory?**
Loaded games stay in memory so they reopen instantly. Right-click a game → **Unload from memory** to free it.

## Notes

- Extracted assets are still owned by the game's creators. Viewing them for personal use is fine, but check before sharing them.
