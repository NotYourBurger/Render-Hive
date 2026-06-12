"""Smoke-test helper (manual): render the current frame small and report the
image's mean RGB, to prove a texture really resolved (pink = missing).
Run:  blender -b --factory-startup <file.blend> -P tests/_smoke_render_check.py -- <out_dir>
Prints RENDER_CHECK:{"mean": [r,g,b], "out": path}
"""
import bpy
import json
import os
import sys

out_dir = os.path.abspath(sys.argv[sys.argv.index("--") + 1])
os.makedirs(out_dir, exist_ok=True)

scene = bpy.context.scene
scene.frame_set(scene.frame_start)
scene.render.resolution_percentage = 25
if scene.render.engine == "CYCLES":
    scene.cycles.samples = 8
    scene.cycles.device = "CPU"
out_path = os.path.join(out_dir, "check.png")
scene.render.filepath = out_path
scene.render.image_settings.file_format = "PNG"
bpy.ops.render.render(write_still=True)

img = bpy.data.images.load(out_path)
px = img.pixels[:]
n = len(px) // 4
mean = [round(sum(px[c::4]) / n, 4) for c in range(3)]
print("RENDER_CHECK:" + json.dumps({"mean": mean, "out": out_path}))
