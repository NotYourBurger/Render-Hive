"""RenderHive frame preview converter — runs INSIDE Blender.

Browsers cannot display EXR or TIFF, so the coordinator converts finished
frames to JPEG for the dashboard thumbnails with:

    blender -b --factory-startup -P preview_frame.py -- <frame_file> <out.jpg>

Blender is used (rather than an image library) because it decodes every EXR
flavor it can render: half/float, all codecs (ZIP, PIZ, DWAA...), multilayer.
"""
import sys

import bpy

_args = sys.argv[sys.argv.index("--") + 1:]
SRC = _args[0]
DEST = _args[1]

# Thumbnails are small; cap the long edge so a 4K EXR doesn't become a
# multi-MB JPEG, while staying large enough to spot render mistakes
MAX_DIM = 1280

img = bpy.data.images.load(SRC)
w, h = img.size
if w and h and max(w, h) > MAX_DIM:
    scale = MAX_DIM / max(w, h)
    img.scale(max(1, round(w * scale)), max(1, round(h * scale)))

scene = bpy.context.scene
scene.render.image_settings.file_format = "JPEG"
scene.render.image_settings.quality = 90
# EXR holds linear scene-referred data; display it with the plain sRGB
# transform (the EXR-viewer convention). Tone-mapped looks like AgX would
# re-grade an image whose look the original scene already chose.
scene.display_settings.display_device = "sRGB"
scene.view_settings.view_transform = "Standard"
scene.view_settings.look = "None"
scene.view_settings.exposure = 0.0
scene.view_settings.gamma = 1.0

img.save_render(DEST, scene=scene)
print("RENDERHIVE_PREVIEW: ok")
