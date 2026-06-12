#!/usr/bin/env python3
"""
RenderHive - Dependency Packer (runs INSIDE Blender)
====================================================
Localizes every external file a .blend depends on into a portable project
folder, the same way Premiere / After Effects "Collect Files" works:

    blender -b scene.blend --factory-startup -P pack_deps.py -- <dest_dir>

What it collects:
  - Image textures        (single files, image sequences, UDIM tiles, videos)
  - Movie clips           (motion tracking / compositor, incl. sequences)
  - VSE strips            (movie strips, image strips; audio via bpy.data.sounds)
  - Volumes               (VDB single files and sequences, incl. # tokens)
  - Cache files           (Alembic/USD - Mesh Sequence Cache + Transform Cache)
  - Fluid sim caches      (whole cache directory)
  - Linked libraries      (one level - nested deps inside libs are not chased)
  - Fonts
  - Point-cache folder    (blendcache_<name> next to the original blend)

Everything is copied into <dest_dir>/assets/..., all datablock paths are
rewritten to relative ("//assets/...") and the blend is saved into <dest_dir>
under its original filename, so the folder is fully self-contained.

Blender resets frame offsets / duration / auto-refresh on some datablocks
when their file path changes, so those settings are snapshotted before the
remap and restored afterwards.

On completion a single machine-readable line is printed:
    RENDERHIVE_PACK:{"ok": true, "copied": N, "missing": [...], ...}
"""

import os
import re
import sys
import json
import shutil
import hashlib

import bpy

# ---------------------------------------------------------------------------
# Arguments / destinations
# ---------------------------------------------------------------------------
argv = sys.argv
_args = argv[argv.index("--") + 1:] if "--" in argv else []
DEST = os.path.abspath(_args[0]) if _args else None
# Optional second arg: filename to save the packed blend as. The coordinator
# stores uploads with a job-id prefix but workers expect the original name.
OUT_NAME = _args[1] if len(_args) > 1 else None
DEPS_NAME = "assets"

copied = []     # (abs_src, rel_dest) pairs
missing = []    # source paths that did not exist
missing_relative = []  # the subset of `missing` stored as relative ("//...")
warnings = []   # non-fatal problems, one string each
_single_map = {}   # (category, abs_src) -> rel path  (dedupe single files)
_dir_map = {}      # (category, abs_src_dir) -> dest dir name (dedupe sequences)
_taken_names = {}  # category -> {filename: abs_src} (collision detection)


def _fail(msg):
    print("RENDERHIVE_PACK:" + json.dumps({"ok": False, "error": msg}))
    sys.exit(1)


def _norm(p):
    return os.path.normcase(os.path.normpath(p))


def _abspath(raw):
    """Blender path -> absolute filesystem path (resolves // against the
    blend's ORIGINAL location, which is where the deps still live)."""
    return os.path.abspath(bpy.path.abspath(raw))


def _rel(category, filename):
    """The relative path string stored into the blend (forward slashes)."""
    return f"//{DEPS_NAME}/{category}/{filename}"


def _copy_file(src, dest_abs):
    os.makedirs(os.path.dirname(dest_abs), exist_ok=True)
    shutil.copy2(src, dest_abs)


def _miss(raw, src_abs):
    """Record a dependency that wasn't found on disk. Relative paths are
    tracked separately: they break when a blend is opened away from its
    original folder (e.g. an uploaded copy), which deserves a clearer error."""
    missing.append(src_abs)
    if isinstance(raw, str) and raw.startswith("//"):
        missing_relative.append(raw)


def localize_file(category, raw):
    """Copy one external file into deps/<category>/ and return its new
    relative path, or None if the source is missing."""
    src = _abspath(raw)
    key = (category, _norm(src))
    if key in _single_map:
        return _single_map[key]
    if not os.path.isfile(src):
        _miss(raw, src)
        return None
    name = os.path.basename(src)
    taken = _taken_names.setdefault(category, {})
    if name in taken and taken[name] != _norm(src):
        # Two different sources share a filename - disambiguate with a hash
        stem, ext = os.path.splitext(name)
        tag = hashlib.md5(_norm(src).encode()).hexdigest()[:6]
        name = f"{stem}_{tag}{ext}"
    taken[name] = _norm(src)
    dest_abs = os.path.join(DEST, DEPS_NAME, category, name)
    _copy_file(src, dest_abs)
    rel = _rel(category, name)
    copied.append((src, rel))
    _single_map[key] = rel
    return rel


def _seq_dir_name(category, src_dir):
    """Stable unique folder name for a sequence's source directory."""
    key = (category, _norm(src_dir))
    if key not in _dir_map:
        base = os.path.basename(src_dir.rstrip("\\/")) or "seq"
        base = re.sub(r"[^\w.-]", "_", base)
        tag = hashlib.md5(_norm(src_dir).encode()).hexdigest()[:6]
        _dir_map[key] = f"{base}_{tag}"
    return _dir_map[key]


def localize_sequence(category, raw):
    """Copy ALL frames of a numbered sequence (the given path is one frame).
    Each sequence gets its own subfolder so frame names can't collide.
    Returns the new relative path of the given frame, or None."""
    src = _abspath(raw)
    src_dir = os.path.dirname(src)
    name = os.path.basename(src)
    m = re.match(r"^(.*?)(\d+)(\.[^.]+)$", name)
    if not m or not os.path.isdir(src_dir):
        return localize_file(category, raw)  # not numbered - single file
    prefix, _digits, ext = m.group(1), m.group(2), m.group(3)
    pat = re.compile(re.escape(prefix) + r"\d+" + re.escape(ext) + r"$",
                     re.IGNORECASE)
    sub = f"{category}/{_seq_dir_name(category, src_dir)}"
    found_self = False
    n = 0
    for f in sorted(os.listdir(src_dir)):
        if not pat.match(f):
            continue
        fsrc = os.path.join(src_dir, f)
        dest_abs = os.path.join(DEST, DEPS_NAME, *sub.split("/"), f)
        if not os.path.exists(dest_abs):  # same dir localized once already
            _copy_file(fsrc, dest_abs)
            copied.append((fsrc, _rel(sub, f)))
        n += 1
        if _norm(fsrc) == _norm(src):
            found_self = True
    if n == 0 or not found_self:
        _miss(raw, src)
        return None
    return _rel(sub, name)


def localize_tokens(category, raw, token_re):
    """Copy all files matching a tokenised path (#### frame tokens or <UDIM>)
    and return the remapped path with the tokens preserved."""
    abs_pattern = _abspath(raw)
    src_dir = os.path.dirname(abs_pattern)
    name = os.path.basename(abs_pattern)
    pat = re.compile(token_re(name) + r"$", re.IGNORECASE)
    if not os.path.isdir(src_dir):
        _miss(raw, abs_pattern)
        return None
    sub = f"{category}/{_seq_dir_name(category, src_dir)}"
    n = 0
    for f in sorted(os.listdir(src_dir)):
        if not pat.match(f):
            continue
        fsrc = os.path.join(src_dir, f)
        dest_abs = os.path.join(DEST, DEPS_NAME, *sub.split("/"), f)
        if not os.path.exists(dest_abs):
            _copy_file(fsrc, dest_abs)
            copied.append((fsrc, _rel(sub, f)))
        n += 1
    if n == 0:
        _miss(raw, abs_pattern)
        return None
    return _rel(sub, name)


def _frame_token_re(name):
    """'smoke_####.vdb' -> regex matching smoke_0001.vdb etc."""
    parts = re.split(r"(#+)", name)
    out = ""
    for p in parts:
        out += r"\d+" if p.startswith("#") else re.escape(p)
    return out


def _udim_token_re(name):
    out = ""
    for p in re.split(r"(<UDIM>|<UVTILE>)", name, flags=re.IGNORECASE):
        out += r"[\w.-]+" if p.upper() in ("<UDIM>", "<UVTILE>") else re.escape(p)
    return out


def localize_dir(category, raw_dir):
    """Copy a whole directory (fluid caches). Returns new relative dir path."""
    src = _abspath(raw_dir)
    if not os.path.isdir(src):
        _miss(raw_dir, src)
        return None
    sub = _seq_dir_name(category, src)
    dest_abs = os.path.join(DEST, DEPS_NAME, category, sub)
    if not os.path.isdir(dest_abs):
        shutil.copytree(src, dest_abs)
        copied.append((src, _rel(category, sub)))
    return _rel(category, sub)


# ---------------------------------------------------------------------------
# Settings snapshots (Blender resets these when file paths change)
# ---------------------------------------------------------------------------
IMAGE_PROPS = ("source",)
IMAGE_USER_PROPS = ("frame_duration", "frame_start", "frame_offset",
                    "use_cyclic", "use_auto_refresh")
CLIP_PROPS = ("frame_start", "frame_offset")
VOLUME_PROPS = ("is_sequence", "frame_start", "frame_offset",
                "frame_duration", "sequence_mode")
STRIP_PROPS = ("frame_start", "frame_final_start", "frame_final_duration")


def _snapshot(owner, props):
    return {p: getattr(owner, p) for p in props if hasattr(owner, p)}


def _restore(owner, snap):
    for p, v in snap.items():
        try:
            if getattr(owner, p) != v:
                setattr(owner, p, v)
        except Exception as e:
            warnings.append(f"could not restore {p} on {owner!r}: {e}")


def iter_image_users():
    """Every ImageUser in the file paired with the image it shows.
    These hold the per-user video/sequence playback settings."""
    for tex in bpy.data.textures:
        if getattr(tex, "image", None) and hasattr(tex, "image_user"):
            yield tex.image, tex.image_user
    for obj in bpy.data.objects:
        if obj.type == "EMPTY" and getattr(obj, "image_user", None) \
                and isinstance(obj.data, bpy.types.Image):
            yield obj.data, obj.image_user
    trees = list(bpy.data.node_groups)
    for coll in (bpy.data.materials, bpy.data.worlds,
                 bpy.data.lights, bpy.data.scenes):
        for db in coll:
            if getattr(db, "node_tree", None):
                trees.append(db.node_tree)
    for tree in trees:
        for node in tree.nodes:
            if getattr(node, "image", None) and hasattr(node, "image_user"):
                yield node.image, node.image_user


def iter_vse_strips():
    for scene in bpy.data.scenes:
        se = scene.sequence_editor
        if not se:
            continue
        # Blender 4.3+ renamed sequences_all -> strips_all
        strips = getattr(se, "strips_all", None) or \
            getattr(se, "sequences_all", None) or []
        for strip in strips:
            yield strip


# ---------------------------------------------------------------------------
# Per-datablock remapping
# ---------------------------------------------------------------------------
def remap_images():
    user_snaps = [(iu, _snapshot(iu, IMAGE_USER_PROPS))
                  for _img, iu in iter_image_users()]
    for img in bpy.data.images:
        try:
            if img.packed_file or not img.filepath:
                continue
            if img.filepath.startswith("<") and img.source != "TILED":
                continue  # generated / viewer images
            snap = _snapshot(img, IMAGE_PROPS)
            if img.source == "MOVIE":
                rel = localize_file("videos", img.filepath)
            elif img.source == "SEQUENCE":
                rel = localize_sequence("image_sequences", img.filepath)
            elif img.source == "TILED":
                rel = localize_tokens("udim", img.filepath, _udim_token_re)
            elif img.source == "FILE":
                rel = localize_file("images", img.filepath)
            else:
                continue
            if rel:
                # filepath_raw rewrites the stored path WITHOUT triggering a
                # reload (a reload would fail here and reset video settings)
                img.filepath_raw = rel
            _restore(img, snap)
        except Exception as e:
            warnings.append(f"image '{img.name}': {e}")
    for iu, snap in user_snaps:
        _restore(iu, snap)


def remap_movieclips():
    for clip in bpy.data.movieclips:
        try:
            if not clip.filepath:
                continue
            snap = _snapshot(clip, CLIP_PROPS)
            if getattr(clip, "source", "MOVIE") == "SEQUENCE":
                rel = localize_sequence("movie_clips", clip.filepath)
            else:
                rel = localize_file("movie_clips", clip.filepath)
            if rel:
                clip.filepath = rel
            _restore(clip, snap)
        except Exception as e:
            warnings.append(f"movieclip '{clip.name}': {e}")


def remap_sounds():
    # Covers VSE audio strips and speaker objects (they reference bpy.data.sounds)
    for snd in bpy.data.sounds:
        try:
            if snd.packed_file or not snd.filepath:
                continue
            rel = localize_file("audio", snd.filepath)
            if rel:
                snd.filepath = rel
        except Exception as e:
            warnings.append(f"sound '{snd.name}': {e}")


def remap_vse_strips():
    for strip in iter_vse_strips():
        try:
            snap = _snapshot(strip, STRIP_PROPS)
            if strip.type == "MOVIE" and strip.filepath:
                rel = localize_file("videos", strip.filepath)
                if rel:
                    strip.filepath = rel
            elif strip.type == "IMAGE" and getattr(strip, "directory", ""):
                src_dir = _abspath(strip.directory)
                sub = f"strip_images/{_seq_dir_name('strip_images', src_dir)}"
                ok = 0
                for el in strip.elements:
                    fsrc = os.path.join(src_dir, el.filename)
                    if not os.path.isfile(fsrc):
                        _miss(strip.directory, fsrc)
                        continue
                    dest_abs = os.path.join(DEST, DEPS_NAME,
                                            *sub.split("/"), el.filename)
                    if not os.path.exists(dest_abs):
                        _copy_file(fsrc, dest_abs)
                        copied.append((fsrc, _rel(sub, el.filename)))
                    ok += 1
                if ok:
                    strip.directory = f"//{DEPS_NAME}/{sub}/"
            _restore(strip, snap)
        except Exception as e:
            warnings.append(f"strip '{getattr(strip, 'name', '?')}': {e}")


def remap_volumes():
    for vol in bpy.data.volumes:
        try:
            raw = vol.filepath
            if not raw:
                continue
            snap = _snapshot(vol, VOLUME_PROPS)
            if "#" in os.path.basename(raw):
                rel = localize_tokens("vdb", raw, _frame_token_re)
            elif getattr(vol, "is_sequence", False):
                rel = localize_sequence("vdb", raw)
            else:
                rel = localize_file("vdb", raw)
            if rel:
                vol.filepath = rel
            _restore(vol, snap)
        except Exception as e:
            warnings.append(f"volume '{vol.name}': {e}")


def remap_cache_files():
    """Alembic / USD caches. A CacheFile datablock backs both the
    Mesh Sequence Cache modifier and the Transform Cache constraint,
    so remapping here fixes both."""
    for cf in getattr(bpy.data, "cache_files", []):
        try:
            if not cf.filepath:
                continue
            rel = localize_file("alembic", cf.filepath)
            if rel:
                cf.filepath = rel
        except Exception as e:
            warnings.append(f"cache_file '{cf.name}': {e}")


def remap_fluid_caches():
    for obj in bpy.data.objects:
        for mod in getattr(obj, "modifiers", []):
            if mod.type != "FLUID":
                continue
            dom = getattr(mod, "domain_settings", None)
            if not dom or not getattr(dom, "cache_directory", ""):
                continue
            try:
                rel = localize_dir("fluid_caches", dom.cache_directory)
                if rel:
                    dom.cache_directory = rel
            except Exception as e:
                warnings.append(f"fluid cache on '{obj.name}': {e}")


def remap_libraries():
    for lib in bpy.data.libraries:
        try:
            if not lib.filepath:
                continue
            rel = localize_file("libraries", lib.filepath)
            if rel:
                lib.filepath = rel
            else:
                warnings.append(f"linked library missing: {lib.filepath} "
                                "(its own dependencies are not collected)")
        except Exception as e:
            warnings.append(f"library '{lib.name}': {e}")


def remap_fonts():
    for font in bpy.data.fonts:
        try:
            if font.packed_file or not font.filepath or \
                    font.filepath == "<builtin>":
                continue
            rel = localize_file("fonts", font.filepath)
            if rel:
                font.filepath = rel
        except Exception as e:
            warnings.append(f"font '{font.name}': {e}")


def copy_point_cache_folder(out_stem):
    """Disk-based point caches (cloth/particles) live in a blendcache_<stem>
    folder next to the blend. Blender matches it by the blend's filename, so
    the copy is named after the OUTPUT blend's stem."""
    src_blend = bpy.data.filepath
    stem = os.path.splitext(os.path.basename(src_blend))[0]
    cache_dir = os.path.join(os.path.dirname(src_blend), f"blendcache_{stem}")
    if os.path.isdir(cache_dir):
        dest = os.path.join(DEST, f"blendcache_{out_stem}")
        if not os.path.isdir(dest):
            shutil.copytree(cache_dir, dest)
            copied.append((cache_dir, f"//blendcache_{out_stem}"))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if not DEST:
        _fail("no destination directory given (pass it after '--')")
    if not bpy.data.filepath:
        _fail("blend file has no path (unsaved file?)")
    os.makedirs(os.path.join(DEST, DEPS_NAME), exist_ok=True)

    remap_images()
    remap_movieclips()
    remap_sounds()
    remap_vse_strips()
    remap_volumes()
    remap_cache_files()
    remap_fluid_caches()
    remap_libraries()
    remap_fonts()

    blend_name = OUT_NAME or os.path.basename(bpy.data.filepath)
    copy_point_cache_folder(os.path.splitext(blend_name)[0])
    out_blend = os.path.join(DEST, blend_name)
    # Paths were already rewritten to //deps/... by hand - a second automatic
    # remap would re-anchor them to the OLD location, so it must stay off.
    bpy.ops.wm.save_as_mainfile(filepath=out_blend, relative_remap=False,
                                compress=False)

    total_bytes = 0
    for root, _dirs, files in os.walk(DEST):
        for f in files:
            try:
                total_bytes += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass

    print("RENDERHIVE_PACK:" + json.dumps({
        "ok": True,
        "blend": blend_name,
        "copied": len(copied),
        "bytes": total_bytes,
        "missing": sorted(set(missing)),
        "missing_relative": sorted(set(missing_relative)),
        "warnings": warnings,
    }))


main()
