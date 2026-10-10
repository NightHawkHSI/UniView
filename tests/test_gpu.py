"""uniview/gpu.py: the per-app GPU preference (Windows only; uses a fake exe name and cleans up)."""

import sys

import pytest

from uniview import gpu


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")
def test_apply_sets_and_removes(monkeypatch):
    fake = r"C:\uniview-test\not-a-real-app.exe"
    monkeypatch.setattr(gpu, "_exe", lambda: fake)
    try:
        assert gpu.apply(True) is True
        assert gpu.current_preference(fake) == gpu.HIGH_PERFORMANCE
        assert gpu.apply(True) is False          # already set
        assert gpu.apply(False) is True
        assert gpu.current_preference(fake) is None
    finally:
        gpu.apply(False)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")
def test_user_choice_wins(monkeypatch):
    import winreg
    fake = r"C:\uniview-test\power-saver.exe"
    monkeypatch.setattr(gpu, "_exe", lambda: fake)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, gpu.KEY) as key:
        winreg.SetValueEx(key, fake, 0, winreg.REG_SZ, "GpuPreference=1;")
    try:
        assert gpu.apply(True) is False
        assert gpu.current_preference(fake) == "GpuPreference=1;"
    finally:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, gpu.KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, fake)


def test_renderer_name():
    class RW:
        def ReportCapabilities(self):
            return "OpenGL vendor string:  NVIDIA\nOpenGL renderer string:  NVIDIA GeForce RTX 3050 Ti/PCIe/SSE2\n"
    assert gpu.renderer_name(RW()) == "NVIDIA GeForce RTX 3050 Ti"
    assert gpu.renderer_name(object()) == ""
