import bpy
import os

# ── helpers ──────────────────────────────────────────────────────────────────

def get_grids(vol_data):
    """Return list of grid names on a volume data-block (2.83+)."""
    return [g.name for g in getattr(vol_data, "grids", [])]


def resolve_frame_path(filepath, frame):
    """Expand Blender's ### frame tokens and bpy.path.abspath."""
    import re
    # Replace runs of '#' with zero-padded frame number
    def pad(m):
        return str(frame).zfill(len(m.group()))
    resolved = re.sub(r'#+' , pad, filepath)
    return bpy.path.abspath(resolved)


def print_separator(label=""):
    print("\n" + "─" * 60)
    if label:
        print(f"  {label}")
        print("─" * 60)


# ── main ─────────────────────────────────────────────────────────────────────

def report_vdb_paths():
    scene   = bpy.context.scene
    frame   = scene.frame_current
    results = []

    for obj in bpy.data.objects:
        # ── 1. Volume objects (File / Sequence source) ──────────────────────
        if obj.type == "VOLUME":
            vol = obj.data
            raw = getattr(vol, "filepath", "")

            if not raw:
                continue

            is_seq  = getattr(vol, "is_sequence", False)
            grids   = get_grids(vol)

            if is_seq:
                # Sequence — resolve current frame path and first/last if set
                cur_path = resolve_frame_path(raw, frame)
                start    = getattr(vol, "frame_start",    None)
                dur      = getattr(vol, "frame_duration",  None)
                offset   = getattr(vol, "frame_offset",    0)

                results.append({
                    "object"  : obj.name,
                    "type"    : "VDB Sequence",
                    "raw"     : bpy.path.abspath(raw),
                    "cur_path": cur_path,
                    "exists"  : os.path.isfile(cur_path),
                    "frame"   : frame,
                    "start"   : start,
                    "duration": dur,
                    "offset"  : offset,
                    "grids"   : grids,
                })
            else:
                # Single file
                abs_path = bpy.path.abspath(raw)
                results.append({
                    "object"  : obj.name,
                    "type"    : "VDB Single",
                    "path"    : abs_path,
                    "exists"  : os.path.isfile(abs_path),
                    "grids"   : grids,
                })

        # ── 2. Fluid / smoke simulation domains baked as VDB ────────────────
        elif obj.type == "MESH":
            for mod in obj.modifiers:
                if mod.type != "FLUID":
                    continue
                flow = getattr(mod, "domain_settings", None)
                if not flow:
                    continue
                cache_dir  = bpy.path.abspath(getattr(flow, "cache_directory", ""))
                file_fmt   = getattr(flow, "cache_data_format", "?")
                is_vdb     = (file_fmt == "OPENVDB")
                results.append({
                    "object"   : obj.name,
                    "type"     : "Fluid Domain (VDB)" if is_vdb else
                                  f"Fluid Domain (format: {file_fmt})",
                    "cache_dir": cache_dir,
                    "is_vdb"   : is_vdb,
                    "exists"   : os.path.isdir(cache_dir),
                })

    # ── print report ────────────────────────────────────────────────────────
    if not results:
        print("\n[VDB Report]  No VDB objects found in the scene.")
        return

    print_separator(f"VDB PATH REPORT  —  scene frame {frame}")

    for r in results:
        print_separator(r["object"])
        print(f"  Type   : {r['type']}")

        if r["type"] == "VDB Single":
            print(f"  Path   : {r['path']}")
            print(f"  Exists : {r['exists']}")
            if r["grids"]:
                print(f"  Grids  : {', '.join(r['grids'])}")

        elif r["type"] == "VDB Sequence":
            print(f"  Pattern: {r['raw']}")
            print(f"  Frame  : {r['frame']}  →  {r['cur_path']}")
            print(f"  Exists : {r['exists']}")
            if r["start"] is not None:
                end = r["start"] + (r["duration"] or 0) - 1
                print(f"  Range  : frames {r['start']} – {end}  (offset {r['offset']})")
            if r["grids"]:
                print(f"  Grids  : {', '.join(r['grids'])}")

        elif "Fluid Domain" in r["type"]:
            print(f"  Cache  : {r['cache_dir']}")
            print(f"  Exists : {r['exists']}")

    print("\n" + "─" * 60 + "\n")


report_vdb_paths()