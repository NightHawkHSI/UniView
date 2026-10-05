# Credits

UniView is built on the work of these projects. Thank you to everyone who made them.

## Libraries bundled with UniView

| Project | Used for | License |
|---|---|---|
| [UnityPy](https://github.com/K0lb3/UnityPy) | Reading Unity asset files | MIT |
| [TypeTreeGeneratorAPI](https://github.com/K0lb3/TypeTreeGenerator) | Reading script fields from game code | MIT |
| [texture2ddecoder](https://github.com/K0lb3/texture2ddecoder), [etcpak](https://github.com/K0lb3/etcpak), [astc_encoder_py](https://github.com/K0lb3/astc-encoder-py) | Texture decoding (used by UnityPy) | MIT |
| [fmod_toolkit](https://github.com/K0lb3/fmod_toolkit), [pyfmodex](https://github.com/tyrylu/pyfmodex) | FMOD sound bank decoding (used by UnityPy) | MIT |
| [tpk_ar](https://github.com/K0lb3/tpk_ar), [archspec](https://github.com/archspec/archspec), [attrs](https://github.com/python-attrs/attrs), [lz4](https://github.com/python-lz4/python-lz4), [Brotli](https://github.com/google/brotli) | UnityPy support libraries | MIT / Apache-2.0 or MIT / MIT / BSD-3-Clause / MIT |
| [trimesh](https://github.com/mikedh/trimesh) | Mesh helpers | MIT |
| [PySide6 / Qt for Python](https://www.qt.io/qt-for-python) | User interface | LGPLv3 (used under LGPLv3; also offered as GPLv2/GPLv3) |
| [PyVista](https://github.com/pyvista/pyvista) / [pyvistaqt](https://github.com/pyvista/pyvistaqt) / [VTK](https://vtk.org/) | 3D viewer | MIT / MIT / BSD-3-Clause |
| [NumPy](https://numpy.org/) | Mesh and texture data | BSD-3-Clause (plus 0BSD, MIT, Zlib, CC0 parts) |
| [Pillow](https://python-pillow.org/) | Image decoding and export | MIT-CMU (HPND) |
| [zstandard](https://github.com/indygreg/python-zstandard) | Zstandard decompression | BSD-3-Clause |
| [Python](https://www.python.org/) | Runtime bundled in `UniView.exe` | PSF License |
| [PyInstaller](https://pyinstaller.org/) | Building `UniView.exe` | GPLv2+ with bootloader exception |

### FMOD

UniView includes `fmod.dll` (through fmod_toolkit) to decode FMOD sound banks.

**FMOD Engine, copyright © Firelight Technologies Pty Ltd.** FMOD is proprietary and is not covered by UniView's GPL license. It is used under the [FMOD End User License Agreement](https://www.fmod.com/legal) (free non-commercial license).

## Code ported into UniView

| Project | What | License |
|---|---|---|
| [ValveResourceFormat](https://github.com/ValveResourceFormat/ValveResourceFormat) | Source 2 KeyValues3 reader (`engines/kv3.py`) and mesh decoder port | MIT |
| [meshoptimizer](https://github.com/zeux/meshoptimizer) (c) Arseny Kapoulkine | Vertex/index buffer decoding (`engines/meshopt.py`) | MIT |

## Optional tools (downloaded on request, not bundled)

| Project | Used for | License |
|---|---|---|
| [vgmstream](https://github.com/vgmstream/vgmstream) | Decoding game audio formats | ISC-style (vgmstream license) |
| [Cpp2IL](https://github.com/SamboyCoding/Cpp2IL) | Rebuilding script classes from IL2CPP games | MIT |
| [ILSpy](https://github.com/icsharpcode/ILSpy) (`ilspycmd`) | Decompiling Mono game scripts to C# | MIT |

## Referenced by exported Unity projects

| Project | Used for | License |
|---|---|---|
| [glTFast](https://github.com/Unity-Technologies/com.unity.cloud.gltfast) | Importing exported GLB models in Unity | Apache 2.0 |

Oodle decompression uses the `oo2core_*_win64.dll` that ships with the game you open. UniView does not include or distribute Oodle.

Each project's full license text is in its own repository (and, for bundled libraries, inside the release's `_internal` folder).
