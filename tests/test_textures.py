"""Which texture a material is drawn with: slot roles from names / shader labels, base texture, tiling."""

from types import SimpleNamespace

import numpy as np
import pytest

from engines.sdk import ALBEDO, NORMAL, OTHER, Material, MeshData, TextureRef
from engines.unity import _loose, texture_role
from uniview import model_display as md_
from uniview import pbr


def ref(slot, name="tex", role=OTHER, label="", **kw):
    return TextureRef(slot, name, SimpleNamespace(key=f"{slot}:{name}", name=name), role, label, **kw)


@pytest.mark.parametrize("prop, label, expected", [
    ("_MainTex", "", (ALBEDO, 0)),
    ("_BumpMap", "", (NORMAL, 0)),
    # Shader Graph: the slot is a GUID, the shader names it (Procelio TexturedMaterialShaderMain)
    ("Texture2D_5238b88858d44dcd93a347a7469191f8", "Base Color Map", (ALBEDO, 1)),
    ("Texture2D_0deb4e210a6d43a098da9601d71be2a9", "Normal Map", (NORMAL, 9)),
    ("Texture2D_edcc851480ac4682958afe6fd0d32012", "Material Map (R = Smooth, G = Metallic, B = AO)", (OTHER, 9)),
    ("Texture2D_92004ccd771e46828df3a88316022201", "Paint Mask", (OTHER, 9)),
    ("Triplanar_Base_Colour", "", (ALBEDO, 1)),
    ("Overlay_Base_Colour", "", (ALBEDO, 2)),
    ("_Main_Texture", "", (ALBEDO, 1)),
    ("_Maintexture", "", (ALBEDO, 1)),
    ("Mesh_AO", "", (OTHER, 9)),
    ("_DiffuseMap", "", (ALBEDO, 1)),
    ("_Texture", "", (OTHER, 9)),
])
def test_texture_role(prop, label, expected):
    assert texture_role(prop, label) == expected


def test_base_texture_skips_data_maps():
    normal = ref("Texture2D_0deb", role=NORMAL, label="Normal Map")
    paint = ref("Texture2D_9200", label="Paint Mask")
    base = ref("Texture2D_5238", role=ALBEDO, label="Base Color Map")
    assert pbr.base_texture(Material("m", [normal, paint, base])) is base
    # nothing that is a picture: draw the plain color instead of a mask
    assert pbr.base_texture(Material("m", [normal, paint, ref("_OcclusionMap")])) is None
    # an unknown slot is still better than nothing
    plain = ref("_Texture")
    assert pbr.base_texture(Material("m", [normal, plain])) is plain


def test_tiling_kept_apart_in_groups():
    mesh = MeshData([[0, 0, 0], [1, 0, 0], [0, 1, 0]] * 2, [[[0, 1, 2]], [[3, 4, 5]]])
    tiled = Material("tiled", [ref("_MainTex", "t", ALBEDO, uv_scale=(4, 2), uv_offset=(0.5, 0))])
    plain = Material("plain", [ref("_MainTex", "t", ALBEDO)])
    groups = md_.texture_groups(mesh, [tiled, plain])
    assert sorted(str(g[5]) for g in groups) == ["((4.0, 2.0), (0.5, 0.0))", "None"]
    assert all(len(g[2]) == 1 for g in groups)
    assert np.array_equal(groups[0][2], [[0, 1, 2]])


def test_loose_names():
    assert _loose("MAT_MOV_Wing Delta") == "matmovwingdelta"
    assert _loose("matmovwingdelta").endswith(_loose("WING_DELTA"))


def test_world_mapped_shaders_skip_tiling():
    from engines.unity import world_mapped
    assert world_mapped("Custom/Trilinearmap", {})
    assert world_mapped("Shader Graphs/WorldspaceTexture", {})
    assert world_mapped("Custom/StaticRock", {"_TriplanarMap": 1.0, "_TriplanarScale": 0.2})
    assert not world_mapped("Custom/StaticRock", {"_TriplanarMap": 0.0, "_TriplanarScale": 0.2})
    assert not world_mapped("Universal Render Pipeline/Lit", {"_Surface": 0.0})
