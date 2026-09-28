"""Which game assemblies get decompiled or copied, and the builder's C# classes for script components."""

import re

from uniview.unity_builder import BUILDER_CS
from uniview.unity_scripts import plan_assemblies


def test_plan_assemblies(tmp_path):
    for name in ("Assembly-CSharp", "Assembly-CSharp-firstpass", "Boxophobic.Utils.Scripts", "Facepunch.Steamworks.Win64",
                 "UnityEngine.CoreModule", "UnityEngine.UI", "Unity.TextMeshPro", "System.Core", "mscorlib",
                 "Mono.Security", "netstandard", "UnityEngine.UI.Tests", "Newtonsoft.Json"):
        (tmp_path / f"{name}.dll").write_bytes(b"")
    decompile, libraries = plan_assemblies(str(tmp_path), ["Assembly-CSharp.dll", "Boxophobic.Utils.Scripts.dll",
                                                            "UnityEngine.UI.dll", "UnityEngine.UI.Tests.dll"])
    assert decompile == ["Assembly-CSharp-firstpass", "Assembly-CSharp", "Boxophobic.Utils.Scripts"]
    assert libraries == ["Facepunch.Steamworks.Win64", "Newtonsoft.Json"]


def test_comp_class_has_script_field():
    comp = re.search(r"public class Comp\s*\{(.*?)\}", BUILDER_CS, re.S).group(1)
    assert {"type", "script", "props"} <= set(re.findall(r"public \w+(?:\[\])? (\w+)", comp))


def test_packages_for():
    from uniview.unity_packages import packages_for
    recommended = {"com.unity.ugui": "2.5.0", "com.unity.timeline": "1.8.12", "com.unity.postprocessing": "3.5.4"}
    got = packages_for(["UnityEngine.UI.dll", "Unity.TextMeshPro.dll", "Unity.Timeline.dll", "Unity.Postprocessing.Runtime",
                        "Unity.InputSystem.dll", "Assembly-CSharp.dll"], recommended)
    assert got == {"com.unity.ugui": "2.5.0", "com.unity.timeline": "1.8.12", "com.unity.postprocessing": "3.5.4"}
    assert packages_for(["Unity.TextMeshPro.dll"], {"com.unity.textmeshpro": "3.0.6", "com.unity.ugui": "1.0.0"}) == \
        {"com.unity.textmeshpro": "3.0.6"}  # older editors still have the separate package


def test_csharp_version_and_renames(tmp_path):
    from uniview.unity_scripts import csharp_version, fix_api_renames
    assert [csharp_version(v) for v in ("6000.5.4f1", "2021.3.1f1", "2020.3.5f1", "2019.4.19f1", "")] == \
        ["CSharp9_0", "CSharp9_0", "CSharp8_0", "CSharp7_3", "CSharp7_3"]
    src = tmp_path / "A" / "Ball.cs"
    src.parent.mkdir()
    src.write_text("PhysicMaterial m; PhysicMaterialCombine c; MyPhysicMaterials x;", encoding="utf-8")
    assert fix_api_renames(str(tmp_path), "2022.3.1f1") == 0
    assert fix_api_renames(str(tmp_path), "6000.5.4f1") == 1
    assert src.read_text(encoding="utf-8") == "PhysicsMaterial m; PhysicsMaterialCombine c; MyPhysicMaterials x;"


def test_failed_layouts_are_not_retried():
    from engines.unity import _aligned_nodes

    class Gen:
        calls = 0

        def get_nodes_up(self, assembly, fullname):
            Gen.calls += 1
            raise KeyError(assembly)

    get = _aligned_nodes(Gen())
    for _ in range(3):
        try:
            get("Assembly-UnityScript", "Foo")
        except KeyError:
            pass
    assert Gen.calls == 1


def test_decompile_can_be_cancelled(tmp_path):
    import sys
    import time
    import pytest
    from uniview.unity_scripts import decompile

    slow = [sys.executable, "-c", "import time; time.sleep(30)"]
    start = time.monotonic()
    with pytest.raises(RuntimeError, match="stopped"):
        decompile(slow, "x.dll", str(tmp_path / "out"), str(tmp_path), cancelled=lambda: True)
    assert time.monotonic() - start < 5


def test_looks_obfuscated(tmp_path):
    from uniview.unity_scripts import looks_obfuscated

    plain, scrambled = tmp_path / "plain", tmp_path / "scrambled"
    plain.mkdir()
    scrambled.mkdir()
    for i in range(30):
        (plain / f"PlayerControllerInputHandler{i}.cs").write_text("")
        (scrambled / (["rZkCodHxnHDKLfTEpwBxOzVzYX", "--q0c5djtfUkEKlWOvR1l6PUq-"][i % 2] + f"{i}.cs")).write_text("")
    assert not looks_obfuscated(str(plain))
    assert looks_obfuscated(str(scrambled))


def test_plugin_meta_is_explicit_and_unvalidated():
    from uniview.unity_scripts import plugin_meta

    meta = plugin_meta("0" * 32)
    assert "PluginImporter:" in meta and "isExplicitlyReferenced: 1" in meta and "validateReferences: 0" in meta
