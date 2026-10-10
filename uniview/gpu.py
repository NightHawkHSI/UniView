"""Which graphics card the app renders on (Windows laptops with two GPUs).

Windows runs python.exe / UniView.exe on the power-saving integrated GPU by default, so the 3D view got Intel UHD
instead of the NVIDIA/AMD card. The per-app choice in Settings > System > Display > Graphics lives in the
registry (HKCU, no admin needed); we set it for our own exe before the first OpenGL window, which makes it apply
straight away. Turning the option off removes our entry (back to the Windows default)."""

import sys

from uniview.constants import log

KEY = r"Software\Microsoft\DirectX\UserGpuPreferences"
HIGH_PERFORMANCE = "GpuPreference=2;"


def _exe():
    return sys.executable


def current_preference(exe=None):
    """The registry value for this exe ("GpuPreference=2;" ...) or None."""
    if sys.platform != "win32":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as key:
            return winreg.QueryValueEx(key, exe or _exe())[0]
    except OSError:
        return None


def apply(prefer_fast):
    """Ask Windows for the high-performance GPU (or stop asking). Call before any OpenGL window exists.
    Returns True when the registry changed. Never raises."""
    if sys.platform != "win32":
        return False
    import winreg
    exe = _exe()
    try:
        value = current_preference(exe)
        if prefer_fast:
            if value == HIGH_PERFORMANCE:
                return False
            if value is not None and value != HIGH_PERFORMANCE and "GpuPreference=" in value:
                return False  # the user picked something in Windows' own settings: theirs wins
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, KEY) as key:
                winreg.SetValueEx(key, exe, 0, winreg.REG_SZ, HIGH_PERFORMANCE)
            log.info("Asked Windows to run %s on the high-performance GPU", exe)
            return True
        if value == HIGH_PERFORMANCE:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, exe)
            log.info("Removed the high-performance GPU request for %s", exe)
            return True
    except OSError as e:
        log.warning("Couldn't set the GPU preference: %s", e)
    return False


def renderer_name(render_window):
    """'NVIDIA GeForce RTX 3050 Ti Laptop GPU' ... from a VTK render window, or ''."""
    try:
        for line in render_window.ReportCapabilities().splitlines():
            if "renderer string" in line:
                return line.split(":", 1)[1].strip().split("/")[0]
    except Exception:
        pass
    return ""
