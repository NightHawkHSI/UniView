# Credits

UniView is built on the work of these projects. Thank you to everyone who made them.

## Libraries bundled with UniView

| Project | Used for | License |
|---|---|---|
| [UnityPy](https://github.com/K0lb3/UnityPy) | Reading Unity asset files | MIT |
| [TypeTreeGeneratorAPI](https://github.com/K0lb3/TypeTreeGenerator) | Reading script fields from game code | MIT |
| [Capstone](https://www.capstone-engine.org/) (`capstone.dll`, inside TypeTreeGeneratorAPI) | Disassembler used by TypeTreeGeneratorAPI | BSD-3-Clause |
| [texture2ddecoder](https://github.com/K0lb3/texture2ddecoder), [etcpak](https://github.com/K0lb3/etcpak), [astc_encoder_py](https://github.com/K0lb3/astc-encoder-py) | Texture decoding (used by UnityPy) | MIT |
| [fmod_toolkit](https://github.com/K0lb3/fmod_toolkit), [pyfmodex](https://github.com/tyrylu/pyfmodex) | FMOD sound bank decoding (used by UnityPy) | MIT |
| [tpk_ar](https://github.com/K0lb3/tpk_ar), [archspec](https://github.com/archspec/archspec), [attrs](https://github.com/python-attrs/attrs), [lz4](https://github.com/python-lz4/python-lz4), [Brotli](https://github.com/google/brotli) | UnityPy support libraries | MIT / Apache-2.0 or MIT / MIT / BSD-3-Clause / MIT |
| [trimesh](https://github.com/mikedh/trimesh) | Mesh helpers | MIT |
| [PySide6 / Qt for Python](https://www.qt.io/qt-for-python) | User interface (Qt Widgets; Qt Quick for the loading screen) | LGPLv3 (used under LGPLv3; also offered as GPLv2/GPLv3) |
| [PyVista](https://github.com/pyvista/pyvista) / [pyvistaqt](https://github.com/pyvista/pyvistaqt) / [VTK](https://vtk.org/) | 3D viewer | MIT / MIT / BSD-3-Clause |
| [QtPy](https://github.com/spyder-ide/qtpy), [scooby](https://github.com/banesullivan/scooby), [pooch](https://github.com/fatiando/pooch), [pyvista-validation](https://pypi.org/project/pyvista-validation/) | PyVista support libraries | MIT / MIT / BSD-3-Clause / MIT |
| [Matplotlib](https://matplotlib.org/), [contourpy](https://github.com/contourpy/contourpy), [kiwisolver](https://github.com/nucleic/kiwi), [cycler](https://github.com/matplotlib/cycler), [fontTools](https://github.com/fonttools/fonttools), [pyparsing](https://github.com/pyparsing/pyparsing), [python-dateutil](https://github.com/dateutil/dateutil), [six](https://github.com/benjaminp/six) | Pulled in by PyVista (color maps) | Matplotlib License (PSF-based) / BSD-3-Clause / BSD-3-Clause / BSD-3-Clause / MIT / MIT / Apache-2.0 + BSD-3-Clause / MIT |
| [pyglet](https://github.com/pyglet/pyglet), [fsspec](https://github.com/fsspec/filesystem_spec) | Pulled in by trimesh | BSD-3-Clause / BSD-3-Clause |
| [Requests](https://github.com/psf/requests), [urllib3](https://github.com/urllib3/urllib3), [idna](https://github.com/kjd/idna), [charset-normalizer](https://github.com/jawah/charset_normalizer), [certifi](https://github.com/certifi/python-certifi) | HTTP support pulled in by the libraries above | Apache-2.0 / MIT / BSD-3-Clause / MIT / **MPL-2.0** |
| [psutil](https://github.com/giampaolo/psutil), [packaging](https://github.com/pypa/packaging), [platformdirs](https://github.com/tox-dev/platformdirs) | Support libraries | BSD-3-Clause / Apache-2.0 or BSD-2-Clause / MIT |
| [NumPy](https://numpy.org/) | Mesh and texture data | BSD-3-Clause (plus 0BSD, MIT, Zlib, CC0 parts) |
| [Pillow](https://python-pillow.org/) | Image decoding and export | MIT-CMU (HPND) |
| [zstandard](https://github.com/indygreg/python-zstandard) | Zstandard decompression | BSD-3-Clause |
| [Python](https://www.python.org/) | Runtime bundled in `UniView.exe`, with the libraries the Windows build of Python includes: [OpenSSL](https://www.openssl.org/), [libffi](https://github.com/libffi/libffi), [Tcl/Tk](https://www.tcl-lang.org/) (splash screen), bzip2, xz, expat, zlib | PSF License / Apache-2.0 / MIT / Tcl license / bzip2 license / 0BSD / MIT / zlib |
| Microsoft Visual C++ runtime (`VCRUNTIME140.dll`) | Needed by Python and the native libraries | Microsoft redistributable, under the terms in Python's license file |
| [PyInstaller](https://pyinstaller.org/) | Building `UniView.exe` | GPLv2+ with bootloader exception |

### FMOD

UniView includes `fmod.dll` (through fmod_toolkit) to decode FMOD sound banks.

**FMOD Engine, copyright © Firelight Technologies Pty Ltd.** FMOD is proprietary and is not covered by UniView's GPL license. It is used under the [FMOD End User License Agreement](https://www.fmod.com/legal) (free non-commercial license). UniView is free, non-commercial software.

### Qt (LGPLv3)

PySide6, Shiboken6 and the Qt libraries are used under the GNU Lesser General Public License v3. They are shipped as separate DLLs in the `_internal` folder, so you can replace them with your own build of the same version. Their source code is available from [qt.io](https://www.qt.io/download-open-source) and [code.qt.io](https://code.qt.io/cgit/pyside/pyside-setup.git/).

## Code ported into UniView

| Project | What | License |
|---|---|---|
| [ValveResourceFormat](https://github.com/ValveResourceFormat/ValveResourceFormat) | Source 2 KeyValues3 reader (`engines/kv3.py`) and mesh decoder port | MIT |
| [meshoptimizer](https://github.com/zeux/meshoptimizer) (c) Arseny Kapoulkine | Vertex/index buffer decoding (`engines/meshopt.py`) | MIT |
| [UnityPy](https://github.com/K0lb3/UnityPy) (c) K0lb3 | A patched copy of UnityPy 1.25's `SerializedFile.__init__` and adjusted bundle reading, for compact object tables in very large games (`engines/unity_lazy.py`) | MIT |

## Inspiration

The loading screen is a homage to the classic **Garry's Mod** loading screen by Facepunch Studios. It is drawn from scratch; no artwork or code from Garry's Mod is used.

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

Each project's full license text is in its own repository. Release builds also include a `licenses` folder next to `UniView.exe` with the license files of every bundled library (collected by `build.py`), including Python's license file, which covers OpenSSL, libffi, Tcl/Tk, bzip2 and the Visual C++ runtime. License texts that the libraries don't ship themselves (LGPLv3 for Qt, Capstone) are kept in [`third_party_licenses`](third_party_licenses).

## Source code

UniView is licensed under the [GNU General Public License v3.0](LICENSE). The complete source code of each release is at [github.com/NightHawkHSI/UniView](https://github.com/NightHawkHSI/UniView), under the tag of that version (e.g. `v2.7.1`).
