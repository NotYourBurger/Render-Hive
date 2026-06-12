"""Smoke-test helper (manual): build a .blend with external dependencies.
Run:  blender -b --factory-startup -P tests/_smoke_make_scene.py -- <workdir>
Creates <workdir>/assets/* dep files and <workdir>/scene.blend referencing
them with ABSOLUTE paths, like a real user project would.
"""
import bpy
import os
import sys

work = os.path.abspath(sys.argv[sys.argv.index("--") + 1])
assets = os.path.join(work, "assets")
os.makedirs(assets, exist_ok=True)

# --- dep files on disk ------------------------------------------------------
video_path = os.path.join(assets, "clip.mp4")
with open(video_path, "wb") as f:
    f.write(b"\x00fake-mp4-bytes")

for i in range(1, 4):
    with open(os.path.join(assets, f"smoke_{i:04d}.vdb"), "wb") as f:
        f.write(b"fake-vdb")
vdb_pattern = os.path.join(assets, "smoke_0001.vdb")

# Real PNG so FILE images keep working
png_path = os.path.join(assets, "tex.png")
tmp_img = bpy.data.images.new("tmp", 8, 8)
tmp_img.filepath_raw = png_path
tmp_img.file_format = "PNG"
tmp_img.save()
bpy.data.images.remove(tmp_img)

# Real Alembic by exporting the default cube animation
abc_path = os.path.join(assets, "anim.abc")
bpy.ops.wm.alembic_export(filepath=abc_path, start=1, end=2,
                          selected=False, export_custom_properties=False)

# --- datablocks referencing them --------------------------------------------
# Video texture on the default cube, with non-default playback settings
vid = bpy.data.images.new("vid", 4, 4)
vid.filepath_raw = video_path
vid.source = "MOVIE"

mat = bpy.data.materials.new("vidmat")
mat.use_nodes = True
node = mat.node_tree.nodes.new("ShaderNodeTexImage")
node.image = vid
node.image_user.frame_duration = 42
node.image_user.frame_start = 7
node.image_user.frame_offset = 3
node.image_user.use_cyclic = True
node.image_user.use_auto_refresh = True
cube = bpy.data.objects.get("Cube")
if cube:
    cube.data.materials.append(mat)

# Plain image texture
tex = bpy.data.images.new("tex", 4, 4)
tex.filepath_raw = png_path
tex.source = "FILE"
node2 = mat.node_tree.nodes.new("ShaderNodeTexImage")
node2.image = tex

# VDB volume sequence
vol = bpy.data.volumes.new("smokevol")
vol.filepath = vdb_pattern
vol.is_sequence = True
vol.frame_start = 1
vol.frame_duration = 3
vol.frame_offset = -1
vol_obj = bpy.data.objects.new("SmokeVolume", vol)
bpy.context.scene.collection.objects.link(vol_obj)

# Alembic cache file + Transform Cache constraint (the case from the request)
bpy.ops.cachefile.open(filepath=abc_path)
cf = bpy.data.cache_files[0]
if cube:
    con = cube.constraints.new("TRANSFORM_CACHE")
    con.cache_file = cf
    if cf.object_paths:
        con.object_path = cf.object_paths[0].path

blend_path = os.path.join(work, "scene.blend")
# Optional "rel" flag: store asset paths relative to the blend ("//assets"),
# which is Blender's default behavior in real projects
use_rel = "rel" in sys.argv
bpy.ops.wm.save_as_mainfile(filepath=blend_path, relative_remap=use_rel)
print("SMOKE_SCENE_OK:" + blend_path)
