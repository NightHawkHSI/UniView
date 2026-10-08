"""PBR maps: which texture is which map, pixel repacking, and how GLB / OBJ / Unity .mat exports attach them."""

import os
import threading
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from engines.sdk import ALBEDO, NORMAL, OTHER, Material, MeshData, TextureRef
from tests.test_glb import parse_glb
from uniview import export as uv
from uniview import pbr
from uniview import unity_materials as um


def ref(slot, name=None, role=OTHER):
    name = name or slot.strip("_$").lower()
    return TextureRef(slot, name, SimpleNamespace(key=name, name=name, uid=f"tex:{name}"), role)


class Session:
    """image(asset) -> the picture registered under its key (gray 128 if none)."""

    def __init__(self, images=None):
        self.lock = threading.RLock()
        self.images = images or {}

    def image(self, asset):
        return self.images.get(asset.key) or Image.new("RGBA", (4, 4), (128, 128, 128, 255))


def quad():
    return MeshData([[0, 0, 0], [1, 0, 0], [1, 1, 0]], [[[0, 1, 2]]], uvs={"UV0": [[0, 0], [1, 0], [1, 1]]})


@pytest.mark.parametrize("slot, name, expected", [
    ("_MainTex", "x", pbr.ALBEDO),
    ("_BumpMap", "x", pbr.NORMAL),
    ("_MetallicGlossMap", "x", pbr.METAL_GLOSS),
    ("_MaskMap", "x", pbr.MASK),
    ("_OcclusionMap", "x", pbr.AO),
    ("_ParallaxMap", "x", pbr.HEIGHT),
    ("_HeightMap", "x", pbr.HEIGHT),
    ("_EmissionMap", "x", pbr.EMISSION),
    ("_SpecGlossMap", "x", pbr.SPECULAR),
    ("_DetailNormalMap", "x", pbr.DETAIL_NORMAL),
    ("g_tRoughness", "x", pbr.ROUGHNESS),
    ("g_tAmbientOcclusion", "x", pbr.AO),
    ("T_Rock_ORM", "T_Rock_ORM", pbr.ORM),       # Unreal: slot = texture name
    ("T_Rock_R", "T_Rock_R", pbr.ROUGHNESS),
    ("T_Rock_H", "T_Rock_H", pbr.HEIGHT),
    ("texture", "rock_AO", pbr.AO),
    ("_CustomTex", "rock_metallic", pbr.METALLIC),
])
def test_assign(slot, name, expected):
    t = ref(slot, name)
    assert pbr.assign([t]) == {expected: t}


def test_assign_roles_win_and_first_one_counts():
    a, n, b = ref("_MainTex", "a", ALBEDO), ref("_MainTex", "n", NORMAL), ref("_BaseMap", "b")
    assert pbr.assign([a, n, b]) == {pbr.ALBEDO: a, pbr.NORMAL: n}
    assert pbr.assign([ref("_MossTex", "moss")]) == {}  # unknown slot, no telling name


def test_unpack_dxt5nm_and_bc5():
    x, y = 200, 90  # stored X/Y
    dxt5nm = Image.new("RGBA", (8, 8), (255, y, y, x))
    dxt5nm.putpixel((0, 0), (255, y, y, x - 10))  # some variation, like a real map
    out = np.asarray(pbr.unpack_normal(dxt5nm))[1, 1]
    nx, ny = x / 255 * 2 - 1, y / 255 * 2 - 1
    assert out[0] == x and out[1] == y
    assert abs(out[2] - (np.sqrt(1 - nx * nx - ny * ny) * 0.5 + 0.5) * 255) <= 1
    bc5 = Image.new("RGBA", (8, 8), (x, y, 0, 255))
    assert tuple(np.asarray(pbr.unpack_normal(bc5))[1, 1]) == tuple(out)
    rgb = Image.new("RGBA", (8, 8), (128, 128, 255, 255))
    assert pbr.normal_layout(rgb) == "rgb"
    assert tuple(np.asarray(pbr.unpack_normal(rgb))[0, 0]) == (128, 128, 255)


def test_flat_dxt5nm_is_recognized():
    assert pbr.normal_layout(Image.new("RGBA", (8, 8), (255, 128, 128, 128))) == "dxt5nm"


def test_gltf_metal_rough_from_unity_and_separate_maps():
    unity = Image.new("RGBA", (2, 2), (255, 0, 0, 51))  # metallic 1, smoothness 0.2
    r, g, b = np.asarray(pbr.gltf_metal_rough(pbr.METAL_GLOSS, unity))[0, 0]
    assert (r, b) == (255, 255) and abs(g - 204) <= 1
    rough = Image.new("RGB", (4, 4), (64, 64, 64))
    metal = Image.new("RGB", (2, 2), (10, 10, 10))  # different size: resized
    px = np.asarray(pbr.gltf_metal_rough(pbr.ROUGHNESS, rough, second=(pbr.METALLIC, metal)))[0, 0]
    assert tuple(px) == (255, 64, 10)
    smooth = Image.new("RGB", (2, 2), (255, 255, 255))
    assert np.asarray(pbr.gltf_metal_rough(pbr.SMOOTHNESS, smooth, gloss_scale=0.5))[0, 0, 1] in (127, 128)


def test_unity_metal_gloss_from_orm_and_roughness():
    orm = Image.new("RGB", (2, 2), (200, 64, 255))
    assert tuple(np.asarray(pbr.unity_metal_gloss(pbr.ORM, orm))[0, 0]) == (255, 255, 255, 191)
    rough = Image.new("RGB", (2, 2), (255, 255, 255))
    assert tuple(np.asarray(pbr.unity_metal_gloss(pbr.ROUGHNESS, rough))[0, 0]) == (0, 0, 0, 0)


def test_glb_attaches_every_map(tmp_path):
    textures = [ref("_MainTex", role=ALBEDO), ref("_BumpMap", role=NORMAL), ref("_MetallicGlossMap"),
                ref("_OcclusionMap"), ref("_EmissionMap"), ref("_ParallaxMap"), ref("_DetailAlbedoMap")]
    mat = Material("M", textures, properties=[("_EmissionColor", "#ff8000  alpha 1.00"), ("_GlossMapScale", "0.5"),
                                              ("_BumpScale", "2")])
    path = str(tmp_path / "m.glb")
    files = uv.write_glb(Session(), quad(), [mat], path)
    gltf, _ = parse_glb(path)
    m = gltf["materials"][0]
    p = m["pbrMetallicRoughness"]
    assert {"baseColorTexture", "metallicRoughnessTexture"} <= set(p)
    assert p["metallicFactor"] == 1.0 and p["roughnessFactor"] == 1.0
    assert m["normalTexture"]["scale"] == 2
    assert "occlusionTexture" in m and "emissiveTexture" in m
    assert m["emissiveFactor"] == [1.0, 128 / 255, 0.0]
    assert len({p["baseColorTexture"]["index"], p["metallicRoughnessTexture"]["index"], m["normalTexture"]["index"],
                m["occlusionTexture"]["index"], m["emissiveTexture"]["index"]}) == 5
    # glTF has no height / detail slot: saved next to the model
    assert sorted(os.path.basename(f) for f in files[1:]) == ["m_detailalbedomap_detail_albedo.png",
                                                             "m_parallaxmap_height.png"]


def test_glb_factors_without_maps_and_orm_used_as_is(tmp_path):
    plain = Material("P", properties=[("_Metallic", "0.3"), ("_Glossiness", "0.75")])
    orm = Material("O", [ref("T_Rock_BC", role=ALBEDO), ref("T_Rock_ORM")])
    path = str(tmp_path / "m.glb")
    uv.write_glb(Session(), quad(), [plain, orm], path)
    gltf, _ = parse_glb(path)
    p = gltf["materials"][0]["pbrMetallicRoughness"]
    assert p["metallicFactor"] == pytest.approx(0.3) and p["roughnessFactor"] == pytest.approx(0.25)
    m = gltf["materials"][1]
    assert m["occlusionTexture"]["index"] == m["pbrMetallicRoughness"]["metallicRoughnessTexture"]["index"]
    assert len(gltf["images"]) == 2


def test_glb_project_mode_references_files_and_skips_repacks(tmp_path):
    textures = [ref("_MainTex", role=ALBEDO), ref("_BumpMap", role=NORMAL), ref("_MetallicGlossMap"),
                ref("_ParallaxMap")]
    path = str(tmp_path / "m.glb")
    files = uv.write_glb(Session(), quad(), [Material("M", textures)], path, image_uri=lambda a: f"{a.key}.png")
    gltf, _ = parse_glb(path)
    assert [i["uri"] for i in gltf["images"]] == ["maintex.png", "bumpmap.png"]
    assert "metallicRoughnessTexture" not in gltf["materials"][0]["pbrMetallicRoughness"]
    assert files == [path]


def test_obj_mtl_links_pbr_maps(tmp_path):
    textures = [ref("_MainTex", role=ALBEDO), ref("_BumpMap", role=NORMAL), ref("_MetallicGlossMap"),
                ref("_EmissionMap"), ref("_ParallaxMap"), ref("_OcclusionMap")]
    dxt5nm = Image.new("RGBA", (4, 4), (255, 128, 128, 200))
    path = str(tmp_path / "m.obj")
    files = uv.write_obj(Session({"bumpmap": dxt5nm}), quad(), [Material("M", textures)], path)
    mtl = open(tmp_path / "m.mtl").read().splitlines()
    keys = {line.split()[0]: line.split()[-1] for line in mtl if line and " " in line}
    assert keys["map_Kd"] == "m_maintex.png" and keys["map_Bump"] == "m_bumpmap.png"
    assert keys["map_Pm"] == "m_metallicglossmap_metallic.png"
    assert keys["map_Pr"] == "m_metallicglossmap_roughness.png"
    assert keys["map_Ke"] == "m_emissionmap.png" and keys["disp"] == "m_parallaxmap.png"
    assert os.path.join(str(tmp_path), "m_occlusionmap.png") in files  # no MTL key, saved anyway
    assert tuple(np.asarray(Image.open(tmp_path / "m_bumpmap.png"))[0, 0][:2]) == (200, 128)  # unpacked


def details(textures, floats=None, colors=None, shader="Custom/Rock"):
    return {"name": "Mat", "shader": shader, "textures": [(p, a, (1, 1), (0, 0)) for p, a in textures],
            "colors": colors or {}, "floats": floats or {}, "keywords": [], "queue": -1, "tags": {}}


def tex(name):
    return SimpleNamespace(uid=f"tex:{name}", key=name, name=name)


def guid(asset):
    return asset.key.ljust(32, "0")[:32]


def test_mat_height_detail_and_mask_occlusion():
    m = um.convert(details([("_BaseMap", tex("a")), ("_MaskMap", tex("mask")), ("_HeightMap", tex("h")),
                            ("_DetailAlbedoMap", tex("da")), ("_DetailNormalMap", tex("dn"))],
                           floats={"_Smoothness": 0.6}), guid)
    t = m["textures"]
    assert t["_MetallicGlossMap"][0] == t["_OcclusionMap"][0] == guid(tex("mask"))
    assert t["_ParallaxMap"][0] == guid(tex("h")) and m["floats"]["_Parallax"] == 0.02
    assert {"_DetailAlbedoMap", "_DetailNormalMap"} <= set(t)
    assert {"_METALLICGLOSSMAP", "_PARALLAXMAP", "_DETAIL_MULX2"} <= set(m["keywords"])
    assert m["floats"]["_GlossMapScale"] == 0.6


def test_mat_repacks_separate_roughness_and_orm():
    calls = []

    def make(recipe, sources):
        calls.append((recipe, [(ch, a.key) for ch, a in sources]))
        return "f" * 32

    m = um.convert(details([("_MainTex", tex("a")), ("_MetallicMap", tex("m")), ("_RoughnessMap", tex("r"))]),
                   guid, make)
    assert calls == [("metal_gloss", [(pbr.METALLIC, "m"), (pbr.ROUGHNESS, "r")])]
    assert m["textures"]["_MetallicGlossMap"][0] == "f" * 32 and m["floats"]["_GlossMapScale"] == 1.0
    calls.clear()
    m = um.convert(details([("_MainTex", tex("a")), ("_ORMTex", tex("orm"))]), guid, make)
    assert [c[0] for c in calls] == ["metal_gloss", "ao"]
    assert "_OcclusionMap" in m["textures"]
    # no callback (or it fails): the material still converts, just without the map
    assert "_MetallicGlossMap" not in um.convert(details([("_RoughnessMap", tex("r"))]), guid)["textures"]


def test_mat_specular_setup():
    m = um.convert(details([("_MainTex", tex("a")), ("_SpecGlossMap", tex("s"))],
                           colors={"_SpecColor": (0.5, 0.5, 0.5, 1)}), guid)
    assert m["shader"] == "Standard (Specular setup)" and m["shader_id"] == 45
    assert "_SPECGLOSSMAP" in m["keywords"] and m["colors"]["_SpecColor"] == (0.5, 0.5, 0.5, 1)


def tiled_ref(slot, name, role):
    return TextureRef(slot, name, SimpleNamespace(key=name, name=name, uid=f"tex:{name}"), role,
                      uv_scale=(4.0, 2.0), uv_offset=(0.25, 0.5))


def test_glb_tiling_as_texture_transform(tmp_path):
    textures = [tiled_ref("_MainTex", "albedo", ALBEDO), ref("_BumpMap", "bump", NORMAL)]
    path = str(tmp_path / "m.glb")
    uv.write_glb(Session(), quad(), [Material("M", textures)], path)
    gltf, _bin = parse_glb(path)
    assert gltf["extensionsUsed"] == ["KHR_texture_transform"]
    m = gltf["materials"][0]
    t = m["pbrMetallicRoughness"]["baseColorTexture"]["extensions"]["KHR_texture_transform"]
    assert t == {"scale": [4.0, 2.0], "offset": [0.25, 1.0 - 2.0 - 0.5]}  # glTF's V runs top-down
    assert m["normalTexture"]["extensions"]["KHR_texture_transform"] == t  # Unity tiles every map like _MainTex


def test_glb_untiled_has_no_extension(tmp_path):
    path = str(tmp_path / "p.glb")
    uv.write_glb(Session(), quad(), [Material("M", [ref("_MainTex", "albedo", ALBEDO)])], path)
    gltf, _bin = parse_glb(path)
    assert "extensionsUsed" not in gltf
    assert "extensions" not in gltf["materials"][0]["pbrMetallicRoughness"]["baseColorTexture"]


def test_obj_tiling_options(tmp_path):
    path = str(tmp_path / "m.obj")
    uv.write_obj(Session(), quad(), [Material("M", [tiled_ref("_MainTex", "albedo", ALBEDO)])], path)
    mtl = open(str(tmp_path / "m.mtl"), encoding="utf-8").read()
    assert "map_Kd -s 4 2 1 -o 0.25 0.5 0 " in mtl
