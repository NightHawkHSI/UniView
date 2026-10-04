from uniview import unity_registry as ur


def version(unity="2020.3", release="0f1", deps=None):
    return {"unity": unity, "unityRelease": release, "dependencies": deps or {}}


FAKE = {
    "com.unity.entities": {"versions": {
        "0.51.1-preview.21": version("2020.3", "30f1", {"com.unity.collections": "0.15.0-preview.21",
                                                         "com.unity.jobs": "0.51.0-preview.32",
                                                         "com.unity.modules.audio": "1.0.0"}),
        "1.0.16": version("2022.3", "0f1", {"com.unity.collections": "2.1.4"}),
    }},
    "com.unity.collections": {"versions": {"0.15.0-preview.21": version(), "1.2.3": version(),
                                           "2.1.4": version("2022.2")}},
    "com.unity.jobs": {"versions": {"0.51.0-preview.32": version(deps={"com.unity.ugui": "1.0.0"})}},
}


def registry():
    return ur.Registry(lambda name: FAKE[name])


def test_semver_order():
    assert sorted(["1.2.3", "1.2.3-preview.4", "1.2.2", "1.10.0", "1.2.3-preview.10"], key=ur.semver_key) == \
        ["1.2.2", "1.2.3-preview.4", "1.2.3-preview.10", "1.2.3", "1.10.0"]


def test_pick_version_fits_the_editor():
    meta = FAKE["com.unity.entities"]
    assert ur.pick_version(meta, "2020.3.20f1") is None                # 0.51.1 needs 2020.3.30f1+
    assert ur.pick_version(meta, "2021.3.5f1") == "0.51.1-preview.21"   # previews when there's no release
    assert ur.pick_version(meta, "6000.5.4f1") == "1.0.16"


def test_resolve_takes_the_highest_version_asked_for():
    got = ur.resolve({"com.unity.entities": "0.51.1-preview.21", "com.unity.collections": "1.2.3"},
                     "2021.3.35f1", builtin={"com.unity.ugui"}, registry=registry())
    assert got == {"com.unity.entities": "0.51.1-preview.21", "com.unity.collections": "1.2.3",
                   "com.unity.jobs": "0.51.0-preview.32"}  # no modules, no built-in ugui
    assert ur.resolve({"com.unity.entities": None}, "6000.5.4f1", registry=registry()) == \
        {"com.unity.entities": "1.0.16", "com.unity.collections": "2.1.4"}
    assert ur.resolve({"com.example.gone": None}, "6000.5.4f1",
                      registry=ur.Registry(lambda name: (_ for _ in ()).throw(OSError("offline")))) == {}


def test_bundle_copies_tarballs_and_drops_old_versions(tmp_path, monkeypatch):
    calls = []
    src = tmp_path / "src.tgz"
    src.write_bytes(b"tgz")

    def fake_path(name, ver, registry=None):
        calls.append(name)
        if name == "com.unity.gone":
            raise OSError("offline")
        return str(src)

    monkeypatch.setattr(ur, "tarball_path", fake_path)
    root = tmp_path / "proj"
    (root / "LocalPackages").mkdir(parents=True)
    (root / "LocalPackages" / "com.unity.entities-0.50.0.tgz").write_bytes(b"old")
    refs, failed = ur.bundle(str(root), {"com.unity.entities": "0.51.1", "com.unity.gone": "1.0.0"})
    assert refs == {"com.unity.entities": "file:../LocalPackages/com.unity.entities-0.51.1.tgz"}
    assert failed == ["com.unity.gone"]
    assert sorted(p.name for p in (root / "LocalPackages").iterdir()) == ["com.unity.entities-0.51.1.tgz"]
    ur.bundle(str(root), {"com.unity.entities": "0.51.1"})  # already there: not fetched again
    assert calls == ["com.unity.entities", "com.unity.gone"]
