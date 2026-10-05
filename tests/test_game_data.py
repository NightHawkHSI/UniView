"""Game data tables that name a prefab, and code links from their fields."""

import json

from engines.sdk import Asset
from uniview import code_links, game_data

BLOCKS = {str(i): {"Type": "Cube", "Name": f"Block{i}", "Data": {"Path": f"Block{i}"}} for i in range(5)}
BLOCKS["50"] = {"Type": "Cube", "Name": "Logic_AND",
                "Data": {"Path": "Logic_AND", "CubeNameKey": "strLogic_AND", "LogicOperation": "And",
                         "GridScale": [1, 1, 1], "IsToggle": False}}
STRINGS = {f"str{i}": f"text {i}" for i in range(60)}
STRINGS["strLogic_AND"] = "AND Logic Gate"


class FakeSession:
    def __init__(self, texts):
        self.assets = [Asset("text", name, name, uid=name) for name in texts]
        self._texts = texts

    def text(self, asset):
        return self._texts[asset.name]


def session():
    return FakeSession({"FunctionalBlockData": json.dumps(BLOCKS), "LocalizedStrings": json.dumps(STRINGS),
                        "readme": "not json"})


def test_lookup_finds_the_whole_entry_and_resolves_strings():
    entries = game_data.entries_for(session(), "Prefab: Data/_Prefabs/Cubes/Logic_AND", [{"name": "Logic_AND"}])
    assert len(entries) == 1
    entry = entries[0]
    assert entry["title"] == "FunctionalBlockData › 50"
    rows = dict(entry["rows"])
    assert rows["Name"] == '"Logic_AND"'
    assert rows["Data.LogicOperation"] == '"And"'
    assert rows["Data.CubeNameKey"] == '"strLogic_AND"  → "AND Logic Gate"'
    assert rows["Data.GridScale"] == "[1, 1, 1]" and rows["Data.IsToggle"] == "false"


def test_no_match_and_names():
    assert game_data.entries_for(session(), "Prefab: Nothing", []) == []
    assert game_data.prefab_names("Prefab: a/b/Crate", [{"name": "CrateRoot"}]) == ["Crate", "CrateRoot"]


SOURCE = """
internal static class WiresLogicFunctions
{
	public delegate void LogicalAndFunction_00000048$PostfixBurstDelegate(ref CommandContext ctx);

	[BurstCompile]
	public static void LogicalAndFunction(ref CommandContext ctx)
	{
		LogicalAndFunction_00000048$BurstDirectCall.Invoke(ref ctx);
	}

	[MonoPInvokeCallback(typeof(CommandDelegate))]
	public static void LogicalAndFunction$BurstManaged(ref CommandContext ctx)
	{
		bool flag = ctx.GetInput(0) >= 0.5f;
		ctx.SetOutput(0, flag ? 1f : 0f);
	}
}
"""


def test_extract_method_prefers_burst_managed_body():
    text = code_links.extract_method(SOURCE, "LogicalANDFunction")
    assert text.startswith("[MonoPInvokeCallback")
    assert "GetInput(0)" in text and text.rstrip().endswith("}")
    assert code_links.extract_method(SOURCE, "LogicalOrFunction") is None


def test_links_need_the_assembly(tmp_path):
    managed = tmp_path / "Game_Data" / "Managed"
    managed.mkdir(parents=True)
    (managed / "Assembly-CSharp.dll").write_bytes(b"")
    rows = [("Data.LogicOperation", '"V3Angle"')]
    assert code_links.links(str(tmp_path), rows) == []
    (managed / "Gamecraft.Blocks.LogicBlock.dll").write_bytes(b"")
    (link,) = code_links.links(str(tmp_path), rows)
    assert link["methods"] == ["LogicalAngleFunction", "MathsAngleFunction"]


def test_list_assemblies_picks_game_code(tmp_path):
    from uniview import decompile_game
    for name in ("Assembly-CSharp", "Assembly-CSharp-firstpass", "Gamecraft.Wires", "Gamecraft.Blocks", "Gamecraft.GUI",
                 "FullGame", "Newtonsoft.Json", "UnityEngine.CoreModule", "System.Core", "Svelto.ECS", "Game.Tests"):
        (tmp_path / f"{name}.dll").write_bytes(b"x")
    found = {n: picked for n, _size, picked in decompile_game.list_assemblies(str(tmp_path))}
    assert "UnityEngine.CoreModule" not in found and "System.Core" not in found and "Game.Tests" not in found
    assert found["Assembly-CSharp"] and found["Gamecraft.Wires"] and found["FullGame"]
    assert not found["Newtonsoft.Json"] and not found["Svelto.ECS"] and not found["Assembly-CSharp-firstpass"]
