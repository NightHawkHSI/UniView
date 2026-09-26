# Writing an engine plugin for UniView

UniView reads every game through an **engine plugin**. The built-in plugins live in
[`engines/`](engines) (Unity, Source, Source 2, Unreal). If your game uses another engine, or a
format the built-in plugin doesn't handle, you can write your own plugin as a single `.py` file.
You don't need to change UniView itself.

## Quick start

1. Copy [`plugins/_template.py`](plugins/_template.py) to `plugins/my_engine.py`, next to
   `UniView.exe` or `unity_viewer.py`. Files that start with `_` are ignored.
2. Change `id` and `name`, then fill in `detect()` and `open()`.
3. Restart UniView. **Help → Engine plugins** shows whether your plugin loaded. Load errors also
   show up there and in the console.
4. Add the game. If detection picks a different engine, right-click the game and choose
   **Read with engine → your plugin**.

As shipped, the template is a working "loose files" engine that shows images, text files and
`.obj` models found in a folder, so you can try it before changing anything.

## How it fits together

```
EnginePlugin            one per engine: "is this folder mine?", "open it"
  └─ open(path) ──► GameSession     one per loaded game
                       ├─ assets: [Asset]          what the list on the left shows
                       ├─ image(asset)  -> PIL.Image
                       ├─ mesh(asset)   -> MeshData
                       ├─ text(asset) / raw(asset)
                       ├─ materials(asset) -> [Material]   (textures shown on / exported with a model)
                       ├─ stats(asset)  -> {"tris":.., "w":.., ...}  (sorting and search filters)
                       ├─ describe(asset) -> [(label, text)]        (info panel)
                       └─ related(asset)  -> (title, [Asset], empty text)  (links under a texture)
```

Your module must define `PLUGIN = MyEnginePlugin()`, or `PLUGINS = [...]` for several engines.
A plugin whose `id` matches a built-in one replaces the built-in plugin.

## EnginePlugin

| member | what it does |
|---|---|
| `id`, `name`, `version`, `author`, `description` | Identity. `id` is saved in `projects.json`. |
| `detect(path) -> int` | Returns 0–100 for how sure you are that `path` is your engine. The highest score wins. Runs for every game on the Projects page, so it must be **fast**: list folders and read a few headers, nothing heavy. Real engines usually return 90+. |
| `game_info(path) -> dict` | `{"engine_version": "...", "detail": "..."}`, shown on the game's box. |
| `count_files(path) -> int` | Number of data files, shown on the box. |
| `open(path, progress) -> GameSession` | Loads the game on a worker thread. Call `progress("text", done, total)` to update the loading screen. Raise an exception with a clear message if the game can't be opened. |
| `label(info)`, `short_version(info)` | Optional. Text for the box and for "Group by engine version". |
| `options` | Optional per-game settings, e.g. `[{"id": "aes_keys", "label": "AES key(s)", "help": "...", "multiline": True}]`. The user fills them in with right-click → **Engine settings...**, and `open()` gets them as `progress.options`. |

## GameSession

Subclass `engines.sdk.GameSession` and implement only what your engine supports. Anything you
leave out raises a clear "not supported" message in the viewer.

* **`self.assets`**: fill it in `open()`. Also set `self.file_count` and, optionally,
  `self.engine_version`. Messages you append to `self.warnings` are shown once after loading.
* **`self.lock`**: the app holds this lock around every call into the session. Thumbnails, stats
  and the UI run on different threads, so your code doesn't need its own locking.
* **`start_background()` / `close()`**: optional. `start_background()` can start indexing work
  after the game loads. `close()` should stop that work and close files when the game is unloaded.
* **`audio(asset)`**: return `(bytes, extension)` in a format the player decodes (wav, mp3, ogg, flac, aac).
  By default the raw file is used when its extension is one of those.
* **`animation_targets(clip)` / `animate(model, clip)`**: for `animation` assets, list the models a clip fits,
  and return an object with `.length` and `.points_at(t)` giving the posed vertices (same order as `mesh(model)`).
  The viewer then plays the animation on the model.
* **`materials_ready()`**: return `False` while a background index is still building. The viewer
  then shows the bare model and adds its textures once the index is ready.

### Asset

```python
Asset(kind, name, key, uid=None, size=None, path="", source="", ref=None, ext="")
```

* `kind` is one of `model`, `texture`, `sprite`, `animation`, `text`, `audio` or `file`. Use `file` for
  anything that can only be exported as-is.
* `key` is any hashable value that is unique within the game. `uid` is a string that stays the
  same between runs, because favorites are saved by it.
* `path` is where the asset lives in the game, with `/` separators. Bulk export can mirror it as
  folders.
* `ref` is for your own use, for example an archive entry or an offset.

### MeshData conventions

`MeshData(points, submeshes, normals=None, uvs=None, colors=None, material_slots=None, name="")`

* **Right-handed, Y up, counter-clockwise front faces.** This is the glTF and Blender-import
  convention. For Z-up engines, use `engines.sdk.z_up_to_y_up()`. For left-handed engines like
  Unity, flip x and reverse the winding.
* If you don't know an engine's winding, `orient_to_normals(points, tris, normals)` picks it from
  the vertex normals.
* **UV origin at the bottom-left.** DirectX-style engines need `v = 1 - v`.
* `submeshes` is a list of `(M, 3)` index arrays, one per material. `material_slots[i]` gives the
  index into `materials()` for submesh `i`.

OBJ and GLB export, Blender, the UV-layout view and thumbnails all work from `MeshData`, so a plugin
gets them automatically.

### Materials

```python
Material("metal_crate", [TextureRef("$basetexture", "crate_color", tex_asset, ALBEDO),
                         TextureRef("$bumpmap", "crate_normal", normal_asset, NORMAL)])
```

The viewer puts the first `ALBEDO` texture of the material with the most triangles on the model.
Exports include all the textures.

## Helpers in `engines.sdk` and friends

* `BinReader(data)` reads little-endian values: `u8/u16/u32/u64/i32/f32/cstr/read/seek`.
* `kind_for_extension(ext)` gives a sensible default kind for loose files, and
  `pil_image_from_bytes(data)` opens png, jpg, tga, dds and more.
* `walk_files(root, exts)` and `listdir_lower(path)` help inside `detect()`.
* `engines.vpk.VPK` / `VirtualFS` read Valve VPK archives and merge several of them with loose folders.
* `engines.unreal.Pak` and `engines.iostore.IoStore` read Unreal `.pak` and `.utoc`/`.ucas` files, including Oodle via a game-shipped `oo2core_*.dll`.
* `engines.ue_package` reads Unreal package headers (Zen and legacy), textures, virtual textures and meshes.
* `engines.aes.AES` decrypts AES-256 ECB with numpy, no crypto package needed.
* `engines.sdk.cache_dir()` is a folder for caching slow indexing results between runs.
* `engines.extdecode.to_wav(data, ext)` decodes game sound formats with vgmstream when it's installed (the default `audio()` already uses it for known extensions).
* For block-compressed textures, Pillow decodes BC1–BC7 with `Image.frombytes(mode, size, data, "bcn", n)`.
  `texture2ddecoder`, installed with UnityPy, also handles ETC, ASTC and PVRTC.

Only the standard library, **numpy**, **Pillow** and what UniView already ships (UnityPy, lz4,
texture2ddecoder, brotli) are guaranteed to be available in the packaged exe.

## What the built-in plugins can and can't do yet

| Engine | Handles | Missing (good first plugin) |
|---|---|---|
| Unity | models with materials, textures, sprites, text, AudioClips, AnimationClips (generic clips play on skinned models) | humanoid (muscle) animation playback |
| Source | .mdl models, .bsp maps with props, .vtf textures, .vmt materials, sounds, text | model animations, map entities/lighting |
| Source 2 | .vmdl_c models + materials, .vtex_c textures, .vsnd_c sounds, text | animations, maps (.vmap_c / world nodes) |
| Fallout 1/2 | .dat archives (DAT1 LZSS / DAT2 zlib), FRM sprites, RIX images, ACM sounds, MSG text | maps, critter art sets as animations |
| Loose files & archives | any folder or zip-style archive: images, sounds, text, .obj/.x models | more archive types (e.g. id .pak, GoldSrc .wad) |
| Unreal | .pak (v3-12) and IoStore with AES keys; textures incl. virtual textures, static and skeletal meshes, Ogg/WAV sounds | Bink Audio and Wwise sounds, animations, DataTables (need property mappings), UE3 .upk |

If you add support for one of these, please send a pull request. Improving a built-in plugin in
`engines/` is welcome as well as adding new ones.
