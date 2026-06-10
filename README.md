# RenderHive — a tiny self-hosted render farm for Blender

A standalone coordinator + worker system. One PC runs the **coordinator** (which
hosts a web dashboard); every PC runs a lightweight **worker**. You upload a
`.blend`, set a frame range, and the farm hands out individual frames to your
GPUs on a pull basis — so your **4090 automatically grabs more frames than your
5060 Ti** without any manual tuning. You watch progress, ETA, and per-node status
live in the browser, then download all frames as a zip.

No Flamenco, no Go services, no database. Two small Python files.

---

## Read this first — what a render farm actually does

Distributed rendering speeds up **animations** (many frames), because each frame
goes to a different GPU. With your 6 cards you get roughly **6× throughput** on a
frame sequence. This tool is built for that.

It does **not** make a **single still frame** render faster — Blender has no clean
built-in way to split one frame's pixels across separate machines over a network.
If your goal is one hero still, the farm won't help; render that on the 4090 alone.
(For stills, the realistic "use all GPUs" path is to put multiple cards in *one*
machine, not across the LAN.)

So: great for animation / turntables / sequences. Not for a single image.

---

## What you need on every PC

1. **Blender 3.0+** installed (4.x recommended for OptiX). 3.0+ is required.
2. **Python 3.9+** installed and on PATH.
3. All PCs on the same LAN (you already have Gigabit — perfect).

---

## Setup (5 minutes)

### 1. Coordinator (pick ONE machine — your main PC is fine)

Copy the whole folder to it and run:

```
start_coordinator.bat
```

It prints something like:

```
Open the dashboard:   http://192.168.1.10:8080
Point each worker at: http://192.168.1.10:8080
```

Note that IP/URL. Open the dashboard URL in any browser on the LAN.
(If Windows Firewall asks, allow Python on private networks.)

### 2. Workers (every rendering PC, including the coordinator PC if you want it
to render too)

Edit `start_worker.bat`, set `SERVER` to the coordinator URL above, then run it:

```
set SERVER=http://192.168.1.10:8080
set GPU=0
set DEVICE=OPTIX
```

Run `start_worker.bat`. The node appears on the dashboard within a couple seconds.

**Machine with two GPUs** (e.g. both 4070 Ti Supers in one box): run one worker
per GPU. Use `start_worker_dual_gpu.bat`, or open two terminals:

```
python worker.py --server http://192.168.1.10:8080 --gpu 0 --device OPTIX
python worker.py --server http://192.168.1.10:8080 --gpu 1 --device OPTIX
```

Mapping your hardware → workers:
| Machine            | command                                   |
|--------------------|-------------------------------------------|
| 4090 box           | `worker.py --server ... --gpu 0`          |
| 3090 box           | `worker.py --server ... --gpu 0`          |
| dual 4070 Ti Super | two workers: `--gpu 0` and `--gpu 1`      |
| 4080 Super box     | `worker.py --server ... --gpu 0`          |
| 5060 Ti box        | `worker.py --server ... --gpu 0`          |

`OPTIX` is the fastest backend on all your RTX cards. Use `CUDA` only if a scene
misbehaves under OptiX.

---

## Rendering a job

1. On the dashboard click **+ New Render Job**.
2. Pick your `.blend`, set frame start/end, choose engine/backend, click **Queue Job**.
3. Watch nodes light up. When done, click **Download .zip** to grab all frames.
4. Turn frames into a video with ffmpeg (or Blender's Video Sequencer):
   ```
   ffmpeg -framerate 24 -i myjob_%04d.png -c:v libx264 -pix_fmt yuv420p out.mp4
   ```

### Getting your assets to travel (important for textured scenes)

Workers each need the scene's assets. Two options:

**A. Pack everything into the .blend (easiest).** In Blender:
`File → External Data → Pack Resources`, save, then upload that .blend.
Good for textures, images, HDRIs. The coordinator sends the file to each node
automatically. Best for most scenes.

**B. Shared network folder (best for heavy scenes).** Put the project on a NAS or
shared drive that every PC can reach at the **same path** (map the same drive
letter on each, e.g. `Z:\`). In the New Job form, leave the file picker empty and
put the full path in **"shared folder path"**. Nothing gets copied; every node
reads from the share. Use this when you have fluid/smoke sim caches, linked
libraries, or very large textures that don't pack.

---

## Features that make it not-annoying

- **Auto load-balancing** — pull-based scheduling; fast GPUs simply take more frames.
- **Job priorities** — Low / Normal / High / Urgent per job; the farm always works
  the highest-priority job first (FIFO within the same priority). Change priority
  any time via the API.
- **Pause / resume** — pause a job (in-flight frames finish, no new ones start) or
  pause an individual render node from the dashboard.
- **Blend-cache affinity** — a worker prefers frames from the job whose .blend it
  already downloaded, avoiding repeated transfers (never overrides priority).
- **Resume** — "Skip already-rendered frames" is on by default, so a re-run only
  fills in the gaps. Crash-safe.
- **Crash recovery** — three independent safety nets reassign lost frames to
  working PCs:
  - *offline*: a node stops heartbeating → its frames requeue within ~60 s
  - *orphaned*: a node crashed mid-frame and came back idle → requeued in ~30 s
    (doesn't count against the frame's retry limit)
  - *stalled*: a node heartbeats but the render makes no progress for 5 min → requeued
- **Work stealing** — a frame stuck on an abnormally slow node is reclaimed when a
  faster node sits idle.
- **Per-frame requeue** — force any failed or stuck frame back into the queue from
  the frame table (↻ button).
- **Live per-frame progress** — sample/tile counts are parsed from Blender and shown.
- **Clear ETA** — computed from the actual speed of the workers on the job, credits
  partial progress of in-flight frames, includes queue wait for jobs behind others,
  and shows the wall-clock finish time ("→ done 14:32").
- **Selectable output folder** — per job in the submit form, plus a farm-wide
  default in ⚙ Settings.
- **Survives coordinator restart** — jobs persist (atomically) to
  `Documents\RenderHive\state.json`.

---

## Command reference

Coordinator:
```
python coordinator.py --port 8080
```

Worker:
```
python worker.py --server URL [--gpu N] [--device OPTIX|CUDA|CPU]
                 [--name NAME] [--blender PATH] [--shared-root PATH]
```
- `--blender` — only needed if auto-detect fails; point it at `blender.exe`.
- `--shared-root` — if your shared path is relative on workers, this is its mount root.

Everything the coordinator stores lives in `Documents\RenderHive\`
(`blends/` = uploaded files, `output/<jobname>/` = rendered frames,
`state.json` / `settings.json` / `config.json` = farm state and settings).
Existing data from the old `./farm_data/` location is migrated automatically on
first run. Set the `RENDERHIVE_DATA_DIR` environment variable to store data
somewhere else.

---

## Troubleshooting

- **Worker can't connect** → firewall on the coordinator PC. Allow Python /
  port 8080 on private networks. Confirm you can open the dashboard URL from the
  worker PC's browser.
- **"could not find Blender"** → pass `--blender "C:\Program Files\Blender Foundation\Blender 4.2\blender.exe"`.
- **Frames render but look wrong / pink textures** → assets didn't travel. Pack
  resources (option A) or use a shared folder (option B).
- **OptiX errors on an older driver** → update the NVIDIA driver, or use `--device CUDA`.
- **A frame keeps failing** → after 3 tries it's marked failed and the job ends as
  "failed"; the per-frame error log is stored on the coordinator. Other frames
  still complete.

---

## Want a true double-click .exe?

The workers run fine as `python worker.py …`. If you'd rather hand a single
executable to each machine, you can package it:
```
pip install pyinstaller
pyinstaller --onefile worker.py
```
Then distribute `dist/worker.exe` and run it with the same `--server`/`--gpu` flags.

## FireDrum
