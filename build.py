"""Build UniView - Unity Asset Viewer.

    py build.py            -> both outputs
    py build.py github     -> Builds/GitHub   : clean copy of the source, ready to push to a repo
    py build.py release    -> Builds/Release  : standalone .exe folder + .zip (no Python needed)

Add a game folder to also self-test the packaged exe against a real game:
    py build.py release --test-game "D:/SteamLibrary/steamapps/common/Robocraft"
"""

import os
import re
import shutil
import subprocess
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
BUILDS = os.path.join(ROOT, "Builds")
WORK = os.path.join(BUILDS, "_work")
APP_NAME = "UniView"
MAIN_SCRIPT = "unity_viewer.py"
UNITYPY_PACKAGES = [
    "UnityPy", "fmod_toolkit", "pyfmodex", "astc_encoder", "archspec",
    "texture2ddecoder", "etcpak", "tpk_ar", "brotli", "lz4",
]

# Files that make up the project (what goes in the GitHub folder).
SOURCE_FILES = [
    "unity_viewer.py", "requirements.txt", "run.bat", "build.bat", "build.py",
    "Icon.png", "README.md", ".gitignore", "ScreenShots",
]


def step(msg):
    print(f"\n=== {msg}", flush=True)


def read_version():
    with open(os.path.join(ROOT, MAIN_SCRIPT), encoding="utf-8") as f:
        match = re.search(r'^__version__\s*=\s*"([^"]+)"', f.read(), re.M)
    if not match:
        sys.exit(f"Could not find __version__ in {MAIN_SCRIPT}")
    return match.group(1)


def clear_folder(path, keep=(".git",)):
    """Empty a folder but keep e.g. a .git repo inside it."""
    os.makedirs(path, exist_ok=True)
    for name in os.listdir(path):
        if name in keep:
            continue
        full = os.path.join(path, name)
        if os.path.isdir(full) and not os.path.islink(full):
            shutil.rmtree(full)
        else:
            os.remove(full)


def build_github():
    step("GitHub folder (source)")
    dest = os.path.join(BUILDS, "GitHub")
    clear_folder(dest)
    for name in SOURCE_FILES:
        src = os.path.join(ROOT, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(dest, name))
            print(f"  copied {name}/")
        elif os.path.exists(src):
            shutil.copy2(src, os.path.join(dest, name))
            print(f"  copied {name}")
        else:
            print(f"  (missing, skipped) {name}")
    print(f"-> {dest}")


def make_ico(png_path, ico_path):
    """Icon.png -> multi-size .ico, padded to a square so it isn't stretched."""
    from PIL import Image
    img = Image.open(png_path).convert("RGBA")
    side = max(img.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    square.save(ico_path, sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])


def ensure_pyinstaller():
    import importlib.util
    if importlib.util.find_spec("PyInstaller") is None:
        step("Installing PyInstaller")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])


def build_release(version, test_game=None):
    ensure_pyinstaller()
    os.makedirs(WORK, exist_ok=True)
    release_dir = os.path.join(BUILDS, "Release")
    os.makedirs(release_dir, exist_ok=True)

    step("Making app.ico from Icon.png")
    icon_png = os.path.join(ROOT, "Icon.png")
    ico = os.path.join(WORK, "app.ico")
    icon_args = []
    if os.path.isfile(icon_png):
        make_ico(icon_png, ico)
        icon_args = ["--icon", ico, "--add-data", f"{icon_png}{os.pathsep}."]
    else:
        print("  Icon.png not found - building without an icon")

    step(f"PyInstaller (v{version}) - this takes a few minutes")
    dist = os.path.join(WORK, "dist")
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
        "--name", APP_NAME,
        "--distpath", dist, "--workpath", os.path.join(WORK, "build"), "--specpath", WORK,
        # UnityPy and the helper libraries it imports at startup, with their DLLs and data files
        # (fmod.dll, archspec's CPU json, decoder binaries...). Missing any = crash on launch.
        *[arg for pkg in UNITYPY_PACKAGES for arg in ("--collect-all", pkg)],
        "--collect-submodules", "vtkmodules",
        # The app uses PySide6; other Qt bindings on the machine would break the build.
        # IPython/jedi get dragged in by optional imports and just add size.
        *[arg for mod in ("PyQt5", "PyQt6", "PySide2", "IPython", "jedi", "parso")
          for arg in ("--exclude-module", mod)],
        *icon_args,
        os.path.join(ROOT, MAIN_SCRIPT),
    ]
    started = time.time()
    subprocess.check_call(cmd, cwd=ROOT)
    print(f"  PyInstaller finished in {time.time() - started:.0f}s")

    folder_name = f"{APP_NAME}-v{version}"
    out_dir = os.path.join(release_dir, folder_name)
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    shutil.move(os.path.join(dist, APP_NAME), out_dir)
    readme = os.path.join(ROOT, "README.md")
    if os.path.isfile(readme):
        shutil.copy2(readme, out_dir)

    step("Self-testing the packaged exe")
    exe = os.path.join(out_dir, f"{APP_NAME}.exe")
    test_cmd = [exe, "--self-test"] + ([test_game] if test_game else [])
    try:
        code = subprocess.call(test_cmd, cwd=out_dir, timeout=300)
    except subprocess.TimeoutExpired:
        # A windowed exe that crashes at startup shows an error box and never exits.
        subprocess.call(["taskkill", "/F", "/T", "/IM", f"{APP_NAME}.exe"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        code = "timeout (the exe probably showed an error window)"
    log_path = os.path.join(out_dir, "viewer.log")
    if os.path.isfile(log_path):
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                if "self-test" in line:
                    print("  " + line.rstrip())
        os.remove(log_path)  # don't ship the test log
    for leftover in ("crash.log", "projects.json"):
        path = os.path.join(out_dir, leftover)
        if os.path.isfile(path) and os.path.getsize(path) == 0:
            os.remove(path)
    if code != 0:
        sys.exit(f"Self-test of the packaged exe FAILED (exit code {code}) - not zipping.")

    step("Zipping")
    zip_path = os.path.join(release_dir, f"{folder_name}-win64.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for dirpath, _dirs, files in os.walk(out_dir):
            for name in files:
                full = os.path.join(dirpath, name)
                zf.write(full, os.path.join(folder_name, os.path.relpath(full, out_dir)))
    size_mb = os.path.getsize(zip_path) / 1e6
    print(f"-> {out_dir}")
    print(f"-> {zip_path} ({size_mb:.0f} MB)")


def main():
    args = sys.argv[1:]
    test_game = None
    if "--test-game" in args:
        i = args.index("--test-game")
        test_game = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]
    targets = set(a.lower() for a in args) or {"github", "release"}
    unknown = targets - {"github", "release"}
    if unknown:
        sys.exit(f"Unknown target(s): {', '.join(unknown)}. Use 'github' and/or 'release'.")

    version = read_version()
    os.makedirs(BUILDS, exist_ok=True)
    print(f"UniView v{version} -> {BUILDS}")
    if "github" in targets:
        build_github()
    if "release" in targets:
        build_release(version, test_game)
    step("Done")


if __name__ == "__main__":
    main()
