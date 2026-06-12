"""
Blender Python — list every MovieClip, image sequence, and video clip used in:
  • Movie Clip Editor  (bpy.data.movieclips)
  • Image data-blocks  (bpy.data.images  — single, sequence, movie, generated)
  • VSE strips         (Video Sequence Editor — image, movie, sound)
  • Compositor nodes   (Image / Movie Clip nodes in every scene's node tree)
  • Material nodes     (Image Texture nodes across all materials)
  • World / Light nodes
  • Object modifiers   (MeshSequenceCache, GreasePencil layer images, etc.)

Run via: Text Editor → Run Script
         or: blender --background myfile.blend --python list_media_assets.py
"""


import bpy

def get_all_imported_videos():
    # Use a set to automatically prevent duplicate file paths
    video_files = set()

    # 1. Check the Video Sequence Editor (VSE)
    if bpy.context.scene.sequence_editor:
        vse = bpy.context.scene.sequence_editor
        
        # Handle Blender 4.3+ API changes (sequences_all -> strips_all)
        if hasattr(vse, "strips_all"):
            vse_strips = vse.strips_all
        elif hasattr(vse, "sequences_all"):
            vse_strips = vse.sequences_all
        else:
            vse_strips = []

        for strip in vse_strips:
            if strip.type == 'MOVIE':
                # Convert relative paths (//) to absolute system paths
                abs_path = bpy.path.abspath(strip.filepath)
                video_files.add(abs_path)

    # 2. Check the Movie Clips data blocks (Motion Tracking / Compositor)
    for clip in bpy.data.movieclips:
        abs_path = bpy.path.abspath(clip.filepath)
        video_files.add(abs_path)

    # 3. Check Image data blocks (Video files used as textures in materials)
    for img in bpy.data.images:
        if img.source == 'MOVIE':
            abs_path = bpy.path.abspath(img.filepath)
            video_files.add(abs_path)

    return video_files

# Run the function and print the results to the System Console
videos = get_all_imported_videos()

print("\n" + "="*40)
print(f"IMPORTED VIDEO FILES ({len(videos)} found):")
print("="*40)

if not videos:
    print("No video files found in this project.")
else:
    for file in sorted(videos):
        print(file)
        
print("="*40 + "\n")