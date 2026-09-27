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
