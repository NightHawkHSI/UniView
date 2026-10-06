"""Build UniView - Game Asset Viewer.

    py build.py            -> both outputs
    py build.py github     -> Builds/GitHub   : clean copy of the source, ready to push to a repo
    py build.py release    -> Builds/Release  : standalone .exe folder + .zip (no Python needed)

Add a game folder to also self-test the packaged exe against a real game:
    py build.py release --test-game "D:/SteamLibrary/steamapps/common/Robocraft"

Every build first runs the lint (ruff), the plugin SDK type check (mypy) and the tests (pytest)
and stops if any fail.
--skip-checks skips them (emergencies only).
"""

import importlib.util
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
VERSION_FILE = os.path.join("uniview", "__init__.py")
UNITYPY_PACKAGES = [
    "UnityPy", "fmod_toolkit", "pyfmodex", "astc_encoder", "archspec",
    "texture2ddecoder", "etcpak", "tpk_ar", "brotli", "lz4",
    "TypeTreeGeneratorAPI",  # reads MonoBehaviour fields from the game's code (native DLLs inside)
    "zstandard",  # Source 2 / Unreal data compressed with zstd
]

# Files that make up the project (what goes in the GitHub folder).
SOURCE_FILES = [
    "unity_viewer.py", "uniview", "requirements.txt", "run.bat", "build.bat", "build.py",
    "Icon.png", "README.md", "LICENSE", "CREDITS.md", ".gitignore", "ScreenShots", "compat.json",
    "engines", "plugins", "PLUGINS.md", "tests", "third_party_licenses",
    ".github", "ruff.toml", "requirements-dev.txt",
]


def step(msg):
    print(f"\n=== {msg}", flush=True)


def read_version():
    with open(os.path.join(ROOT, VERSION_FILE), encoding="utf-8") as f:
        match = re.search(r'^__version__\s*=\s*"([^"]+)"', f.read(), re.M)
    if not match:
        sys.exit(f"Could not find __version__ in {VERSION_FILE}")
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


def run_checks():
    """Lint + tests (what CI would run). Exits if anything fails."""
    step("Checks (ruff + mypy + pytest)")
    missing = [m for m in ("ruff", "mypy", "pytest") if importlib.util.find_spec(m) is None]
    if missing:
        sys.exit(f"Missing {', '.join(missing)}. Install the dev tools: py -m pip install -r requirements-dev.txt")
    # mypy: the plugin SDK and plugins/ (the contract third-party plugins are written against).
    mypy = ["mypy", "--ignore-missing-imports", "--follow-imports=silent",
            "--check-untyped-defs", "engines/sdk.py", "plugins"]
    for cmd in (["ruff", "check", "."], mypy, ["pytest", "tests", "-q"]):
        if subprocess.run([sys.executable, "-m", *cmd], cwd=ROOT).returncode != 0:
            sys.exit(f"{cmd[0]} failed - fix the problems above (or build with --skip-checks).")


def build_github():
    step("GitHub folder (source)")
    dest = os.path.join(BUILDS, "GitHub")
    clear_folder(dest)
    for name in SOURCE_FILES:
        src = os.path.join(ROOT, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(dest, name), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
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


def make_splash(png_path, out_path, version):
    """Splash image the exe's launcher shows right away, before Python and the big libraries load
    (a first launch can take ~20 s while Windows scans every bundled DLL). UniView closes it when its
    window appears."""
    from PIL import Image, ImageDraw, ImageFont

    def font(size, bold=False):
        for name in (("segoeuib.ttf" if bold else "segoeui.ttf"), "arial.ttf"):
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
        return ImageFont.load_default()

    width, height = 460, 200
    img = Image.new("RGB", (width, height), (32, 33, 36))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, width - 1, height - 1], outline=(67, 160, 71), width=2)
    if os.path.isfile(png_path):
        icon = Image.open(png_path).convert("RGBA")
        icon.thumbnail((112, 112), Image.LANCZOS)
        img.paste(icon, (28, (height - icon.height) // 2), icon)
    x = 164
    draw.text((x, 46), APP_NAME, font=font(34, bold=True), fill=(240, 240, 240))
    draw.text((x, 92), f"Game Asset Viewer  v{version}", font=font(15), fill=(170, 170, 170))
    draw.text((x, 132), "Loading...", font=font(16, bold=True), fill=(67, 160, 71))
    draw.text((x, 156), "The first launch can take a moment.", font=font(13), fill=(140, 140, 140))
    img.save(out_path)


LICENSE_FILE = re.compile(r"(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", re.I)
# Licence texts the bundled pieces need but don't ship themselves (third_party_licenses/<file> -> licenses/<folder>).
EXTRA_LICENSES = {
    "LGPL-3.0.txt": "PySide6 (Qt for Python, Qt, Shiboken6)",  # the wheels only carry the Qt commercial notice
    "capstone.txt": "Capstone (capstone.dll in TypeTreeGeneratorAPI)",
}


def collect_licenses(out_dir, pyz_toc=None):
    """licenses/ next to the exe: the licence files of every installed package that PyInstaller put in
    _internal, Python's own LICENSE.txt (which also covers OpenSSL, libffi, Tcl/Tk, bzip2 and the VC++
    runtime of the Windows build) and the texts in third_party_licenses/. pyz_toc: PyInstaller's PYZ-00.toc,
    which lists the pure-Python modules packed inside the exe. Returns the folder names."""
    import ast
    import importlib.metadata
    internal = os.path.join(out_dir, "_internal")
    bundled = {os.path.splitext(n)[0].lower() for n in os.listdir(internal)}
    if pyz_toc and os.path.isfile(pyz_toc):
        with open(pyz_toc, encoding="utf-8") as f:
            bundled |= {entry[0].split(".")[0].lower() for entry in ast.literal_eval(f.read())[1]}
    dest_root = os.path.join(out_dir, "licenses")
    if os.path.exists(dest_root):
        shutil.rmtree(dest_root)
    made = []

    def copy(src, folder, name):
        target = os.path.join(dest_root, folder)
        os.makedirs(target, exist_ok=True)
        shutil.copy2(src, os.path.join(target, name))
        if folder not in made:
            made.append(folder)

    for dist in importlib.metadata.distributions():
        files = dist.files or []
        tops = (dist.read_text("top_level.txt") or "").split()
        tops += [f.parts[0] for f in files if len(f.parts) > 1 and not f.parts[0].endswith(
            (".dist-info", ".egg-info", ".data")) and f.parts[0] != ".."]
        if not any(t.lower() in bundled for t in tops):
            continue
        folder = f"{dist.metadata['Name']} {dist.version}"
        for f in files:
            if LICENSE_FILE.search(f.name):
                parts = list(f.parts)
                if "licenses" in parts:  # keep the paths numpy & co. use to tell their vendored licences apart
                    parts = parts[parts.index("licenses") + 1:]
                elif parts[0].endswith(".dist-info"):
                    parts = parts[1:]
                copy(dist.locate_file(f), folder, "__".join(parts))
    python_license = os.path.join(sys.base_prefix, "LICENSE.txt")
    if os.path.isfile(python_license):
        copy(python_license, f"Python {sys.version.split()[0]}", "LICENSE.txt")
    for name, folder in EXTRA_LICENSES.items():
        src = os.path.join(ROOT, "third_party_licenses", name)
        if os.path.isfile(src):
            copy(src, folder, name)
    with open(os.path.join(dest_root, "README.txt"), "w", encoding="utf-8") as f:
        f.write("Licences of the third-party software bundled with UniView (see CREDITS.md for what each one does).\n"
                "FMOD (fmod.dll) is proprietary and used under the FMOD End User License Agreement:\n"
                "https://www.fmod.com/legal\n\n" + "\n".join(sorted(made, key=str.lower)) + "\n")
    return made


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
    compat = os.path.join(ROOT, "compat.json")
    if os.path.isfile(compat):
        icon_args += ["--add-data", f"{compat}{os.pathsep}."]
    else:
        print("  Icon.png not found - building without an icon")
    splash = os.path.join(WORK, "splash.png")
    make_splash(icon_png, splash, version)
    icon_args += ["--splash", splash]

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
        # Built-in engine plugins are imported by name at runtime, so list them explicitly.
        "--paths", ROOT, "--collect-submodules", "engines", "--collect-submodules", "uniview",
        # The app uses PySide6; other Qt bindings on the machine would break the build.
        # IPython/jedi get dragged in by optional imports and just add size. The dev tools too, and
        # mypy must stay out: pyvista imports it if present, and a bundled mypy crashes on launch.
        *[arg for mod in ("PyQt5", "PyQt6", "PySide2", "IPython", "jedi", "parso",
                          "mypy", "mypyc", "pytest", "_pytest")
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
    for name in ("README.md", "LICENSE", "CREDITS.md"):
        src = os.path.join(ROOT, name)
        if os.path.isfile(src):
            shutil.copy2(src, out_dir)
    step("Collecting third-party licences")
    made = collect_licenses(out_dir, os.path.join(WORK, "build", APP_NAME, "PYZ-00.toc"))
    print(f"  {len(made)} packages -> licenses/")
    # User plugins live next to the exe: ship the template and the guide.
    os.makedirs(os.path.join(out_dir, "plugins"), exist_ok=True)
    for name, target in (("plugins/_template.py", "plugins/_template.py"), ("PLUGINS.md", "PLUGINS.md")):
        src = os.path.join(ROOT, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(out_dir, target))

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
    # Per-user files the test run wrote (settings.json holds this PC's folders, e.g. C:\Users\<name>):
    # never ship them - the app creates its own on first run.
    for leftover in ("crash.log", "projects.json", "settings.json", "compat_cache.json"):
        path = os.path.join(out_dir, leftover)
        if os.path.isfile(path):
            os.remove(path)
    for leftover in ("cache", "tools"):
        shutil.rmtree(os.path.join(out_dir, leftover), ignore_errors=True)
    # pip's record of where a package was installed from (a local wheel's path names this PC's user).
    for dirpath, _dirs, files in os.walk(out_dir):
        if dirpath.endswith(".dist-info") and "direct_url.json" in files:
            os.remove(os.path.join(dirpath, "direct_url.json"))
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
    skip_checks = "--skip-checks" in args
    args = [a for a in args if a != "--skip-checks"]
    targets = set(a.lower() for a in args) or {"github", "release"}
    unknown = targets - {"github", "release"}
    if unknown:
        sys.exit(f"Unknown target(s): {', '.join(unknown)}. Use 'github' and/or 'release'.")

    version = read_version()
    os.makedirs(BUILDS, exist_ok=True)
    print(f"UniView v{version} -> {BUILDS}")
    if skip_checks:
        print("\n!!! Skipping lint and tests (--skip-checks)")
    else:
        run_checks()
    if "github" in targets:
        build_github()
    if "release" in targets:
        build_release(version, test_game)
    step("Done")


if __name__ == "__main__":
    main()
