"""Updates from GitHub releases: version order, reading the release, checked download, safe unpacking."""

import hashlib
import io
import zipfile

import pytest

from uniview import updater

RELEASE = {
    "tag_name": "v2.19.0", "html_url": "https://github.com/NightHawkHSI/UniView/releases/tag/v2.19.0",
    "body": "Notes", "draft": False, "prerelease": False,
    "assets": [{"name": "UniView-v2.19.0-win64.zip", "size": 3, "digest": "sha256:" + "ab" * 32,
                "browser_download_url": "https://github.com/x/UniView-v2.19.0-win64.zip"}],
}


def test_versions():
    assert updater.is_newer("2.19.0", "2.18.0")
    assert updater.is_newer("v2.18.1", "2.18.0")
    assert not updater.is_newer("2.18.0", "2.18.0")
    assert not updater.is_newer("2.9.9", "2.18.0")
    assert updater.is_newer("2.18.0", "2.18.0-beta")
    assert updater.is_newer("3", "2.99.99")


def test_parse_release():
    r = updater.parse_release(RELEASE)
    assert (r.version, r.tag, r.asset_name, r.asset_size, r.sha256) == (
        "2.19.0", "v2.19.0", "UniView-v2.19.0-win64.zip", 3, "ab" * 32)
    with pytest.raises(updater.UpdateError):
        updater.parse_release(dict(RELEASE, prerelease=True))
    with pytest.raises(updater.UpdateError):
        updater.parse_release({"message": "Not Found"})
    assert updater.parse_release(dict(RELEASE, assets=[])).asset_url == ""


class _Response(io.BytesIO):
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_download_checks_the_checksum(tmp_path, monkeypatch):
    data = b"zip"
    monkeypatch.setattr(updater, "_open", lambda url, timeout: _Response(data))
    good = updater.parse_release(RELEASE)
    good.sha256 = hashlib.sha256(data).hexdigest()
    seen = []
    path = updater.download(good, str(tmp_path), progress=lambda d, t: seen.append(d))
    assert open(path, "rb").read() == data and seen == [3]
    bad = updater.parse_release(RELEASE)  # digest "abab..." doesn't match
    with pytest.raises(updater.UpdateError):
        updater.download(bad, str(tmp_path))
    bad.sha256 = ""
    with pytest.raises(updater.UpdateError):
        updater.download(bad, str(tmp_path))


def test_unpack(tmp_path):
    zpath = tmp_path / "u.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("UniView-v2.19.0/UniView.exe", b"MZ")
        z.writestr("UniView-v2.19.0/_internal/x.dll", b"x")
    folder = updater.unpack(str(zpath), str(tmp_path / "new"))
    assert folder.endswith("UniView-v2.19.0")
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("../evil.exe", b"MZ")
    with pytest.raises(updater.UpdateError):
        updater.unpack(str(zpath), str(tmp_path / "new"))


def test_apply_script():
    script = updater.apply_script(r"C:\T\UniView-update\new\UniView-v2", r"D:\Apps\UniView", "UniView.exe", 1234)
    assert 'PID eq 1234' in script
    assert r'robocopy.exe" "%NEW%\_internal" "%APP%\_internal" /MIR /IS /IT' in script
    assert '/XD "%NEW%\\_internal"' in script
    assert script.count('start "" "%APP%\\UniView.exe"') == 2  # also restarts the old version if copying fails
    assert "\r\n" in script


def test_writable(tmp_path):
    assert updater.writable(str(tmp_path))
    assert not updater.writable(str(tmp_path / "missing"))


def test_only_the_exe_updates_itself():
    assert not updater.can_self_update()  # tests run from source
    with pytest.raises(updater.UpdateError):
        updater.apply("x")
