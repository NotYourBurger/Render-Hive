import bpy, json, sys
s = bpy.context.scene
info = {
    'frame_start': s.frame_start,
    'frame_end': s.frame_end,
    'frame_step': s.frame_step,
    'engine': s.render.engine,
    'format': s.render.image_settings.file_format,
    'resolution_x': s.render.resolution_x,
    'resolution_y': s.render.resolution_y,
}
if s.render.engine == 'CYCLES':
    info['samples'] = s.cycles.samples
else:
    info['samples'] = 0
print('RENDERHIVE_PROBE:' + json.dumps(info))
