"""Smoke-test helper (manual): dump every video/image texture's path and
playback settings as one JSON line, for diffing an original blend against
its packed copy.
Run:  blender -b --factory-startup <file.blend> -P tests/_smoke_dump_videos.py
"""
import bpy
import json
import os

out = {"images": {}, "users": []}

for img in bpy.data.images:
    if img.source not in ("MOVIE", "FILE", "SEQUENCE", "TILED"):
        continue
    out["images"][img.name] = {
        "source": img.source,
        "filepath": img.filepath.replace("\\", "/"),
        "resolved_exists": os.path.isfile(bpy.path.abspath(img.filepath)),
    }


def walk_trees():
    for coll in (bpy.data.materials, bpy.data.worlds, bpy.data.lights,
                 bpy.data.scenes):
        for db in coll:
            if getattr(db, "node_tree", None):
                yield db.name, db.node_tree
    for g in bpy.data.node_groups:
        yield g.name, g


for owner, tree in walk_trees():
    for node in tree.nodes:
        if getattr(node, "image", None) and hasattr(node, "image_user"):
            iu = node.image_user
            out["users"].append({
                "owner": owner, "node": node.name, "image": node.image.name,
                "frame_duration": iu.frame_duration,
                "frame_start": iu.frame_start,
                "frame_offset": iu.frame_offset,
                "use_cyclic": iu.use_cyclic,
                "use_auto_refresh": iu.use_auto_refresh,
            })

print("VIDEO_DUMP:" + json.dumps(out, sort_keys=True))
