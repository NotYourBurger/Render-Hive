"""Smoke-test helper (manual): verify a packed blend produced by pack_deps.py.
Run:  blender -b --factory-startup <pack>/scene.blend -P tests/_smoke_verify_pack.py
Checks every path was remapped to //deps/... and that the video playback
settings survived the relink. Prints SMOKE_VERIFY_OK or raises.
"""
import bpy
import os

errors = []


def check(cond, msg):
    if not cond:
        errors.append(msg)


def norm(p):
    # Blender may flip //deps/... to //deps\... on save - separator-agnostic
    return p.replace("\\", "/")


vid = bpy.data.images.get("vid")
check(vid is not None, "video image datablock missing")
if vid:
    check(norm(vid.filepath).startswith("//assets/videos/"),
          f"video not remapped: {vid.filepath}")
    check(vid.source == "MOVIE", f"video source reset to {vid.source}")
    check(os.path.isfile(bpy.path.abspath(vid.filepath)),
          "remapped video file does not exist on disk")

mat = bpy.data.materials.get("vidmat")
node = next((n for n in mat.node_tree.nodes
             if getattr(n, "image", None) and n.image.name == "vid"), None)
check(node is not None, "video texture node missing")
if node:
    iu = node.image_user
    check(iu.frame_duration == 42, f"frame_duration reset: {iu.frame_duration}")
    check(iu.frame_start == 7, f"frame_start reset: {iu.frame_start}")
    check(iu.frame_offset == 3, f"frame_offset reset: {iu.frame_offset}")
    check(iu.use_cyclic is True, "use_cyclic reset")
    check(iu.use_auto_refresh is True, "use_auto_refresh reset")

tex = bpy.data.images.get("tex")
if tex:
    check(norm(tex.filepath).startswith("//assets/images/"),
          f"image not remapped: {tex.filepath}")
    check(os.path.isfile(bpy.path.abspath(tex.filepath)),
          "remapped image file does not exist on disk")

vol = bpy.data.volumes.get("smokevol")
check(vol is not None, "volume datablock missing")
if vol:
    check(norm(vol.filepath).startswith("//assets/vdb/"),
          f"volume not remapped: {vol.filepath}")
    check(vol.is_sequence, "volume is_sequence reset")
    check(vol.frame_duration == 3, f"volume frame_duration reset: {vol.frame_duration}")
    check(vol.frame_offset == -1, f"volume frame_offset reset: {vol.frame_offset}")
    vdir = os.path.dirname(bpy.path.abspath(vol.filepath))
    n_vdb = len([f for f in os.listdir(vdir) if f.endswith(".vdb")])
    check(n_vdb == 3, f"expected 3 vdb sequence files, found {n_vdb}")

check(len(bpy.data.cache_files) == 1, "cache_file datablock missing")
for cf in bpy.data.cache_files:
    check(norm(cf.filepath).startswith("//assets/alembic/"),
          f"alembic not remapped: {cf.filepath}")
    check(os.path.isfile(bpy.path.abspath(cf.filepath)),
          "remapped alembic file does not exist on disk")

cube = bpy.data.objects.get("Cube")
if cube:
    con = next((c for c in cube.constraints
                if c.type == "TRANSFORM_CACHE"), None)
    check(con is not None and con.cache_file is not None,
          "transform cache constraint lost its cache file")

if errors:
    for e in errors:
        print("SMOKE_FAIL:", e)
    raise SystemExit(1)
print("SMOKE_VERIFY_OK")

