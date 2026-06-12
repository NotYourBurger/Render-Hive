# RenderHive 🐝 — Free Self-Hosted Blender Render Farm (Peer-to-Peer, Zero Config)

![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![Blender 4.x–5.x](https://img.shields.io/badge/blender-4.x%20%E2%80%93%205.x-orange)
![Platform: Windows](https://img.shields.io/badge/platform-windows-lightgrey)
![Dependencies: 2](https://img.shields.io/badge/dependencies-flask%20%2B%20requests-green)

**RenderHive** is a free, open-source, self-hosted **distributed render farm for Blender**.
Run one script on every PC in your LAN and they find each other automatically over UDP —
no server setup, no database, no Docker, no config files. Every node is both a
**coordinator** (with a live web dashboard) and a **GPU worker**, frames are handed out
on a pull basis so your RTX 4090 naturally takes more frames than your 5060 Ti, and a
built-in **🍯 Honey credit system** rewards the PCs that contribute render power to the hive.

A lightweight alternative to Flamenco, SheepIt, or commercial render farm software —
ideal for home studios, small teams, classrooms, and anyone with a few spare GPUs and
a Blender animation to render.

```
start_renderhive.bat        ← run this on every PC. That's the whole setup.
```

---

## Table of Contents

- [Why RenderHive?](#why-renderhive)
- [Feature Highlights](#feature-highlights)
- [The Honey Economy — render credits & loans](#-the-honey-economy--render-credits--loans)
- [Quick Start](#quick-start)
- [Submitting a Render Job](#submitting-a-render-job)
- [Dependency Packing — no more pink textures](#dependency-packing--no-more-pink-textures)
- [EXR & TIFF Previews in the Browser](#exr--tiff-previews-in-the-browser)
- [How It Works (Architecture)](#how-it-works-architecture)
- [Command Reference](#command-reference)
- [Troubleshooting & FAQ](#troubleshooting--faq)

---

## Why RenderHive?

| | RenderHive |
|---|---|
| **Setup** | One `.bat` / one Python script per PC. Peers auto-discover over the LAN. |
| **Infrastructure** | None. No database, no broker, no cloud account. Two pip packages (`flask`, `requests`). |
| **Scheduling** | Pull-based: fast GPUs automatically grab more frames. No manual node tuning. |
| **Assets** | Collects videos, VDB volumes, Alembic caches & more — things Blender's own *Pack Resources* can't pack. |
| **Fairness** | 🍯 Honey credits: rendering for others earns the right to use their GPUs. |
| **Monitoring** | Live web dashboard: per-frame progress, ETAs, node status, frame previews (even EXR). |

Distributed rendering accelerates **animations and frame sequences** — each frame goes to a
different GPU, so six cards ≈ 6× throughput. It does **not** split a *single still image*
across machines (Blender has no clean way to do that over a network); render hero stills on
your fastest single GPU instead.

---

## Feature Highlights

- 🌐 **Zero-config peer discovery** — nodes broadcast on UDP and form a render farm mesh automatically; the dashboard shows the live network topology.
- ⚡ **Pull-based GPU load balancing** — workers request frames when free; multi-GPU boxes spawn one worker per GPU automatically.
- 🍯 **Honey render-credit economy with loans** — earn 1 honey per frame rendered for the hive, spend 1 per frame of your own jobs ([details below](#-the-honey-economy--render-credits--loans)).
- 📦 **"Collect Files"-style dependency packing** — videos used as textures, image sequences, UDIM tiles, movie clips, VSE strips, audio, **VDB volume sequences**, **Alembic/USD caches**, fluid caches, linked libraries and fonts all travel with the job.
- 🖼️ **EXR / TIFF thumbnails in the browser** — frames are converted to cached JPEG previews via headless Blender, so you can spot the forgotten default cube before the whole sequence finishes.
- 🏃 **Frame prefetch & SSE push** — the next frame is downloaded while the current one renders; idle workers are woken by server-sent events instead of polling.
- 🛟 **Three-layer crash recovery** — offline nodes (~60 s), orphaned frames (~30 s) and stalled renders (5 min) are requeued automatically; work-stealing reclaims frames from abnormally slow nodes.
- 🎚️ **Job priorities, pause/resume, per-frame requeue** — Low → Urgent priorities with FIFO within each level; pause jobs or individual nodes from the dashboard.
- ⏱️ **Honest ETAs** — computed from measured worker speed, credits in-flight progress, includes queue wait, and shows wall-clock finish time ("→ done 14:32").
- 💾 **Crash-safe persistence** — jobs, frames and the honey ledger survive coordinator restarts (atomic writes to `Documents\RenderHive\state.json`).
- 🔁 **Resume / skip existing** — re-running a job only renders the missing frames.

---

## 🍯 The Honey Economy — render credits & loans

Honey is RenderHive's built-in points system that keeps a shared farm fair: the people
who contribute GPU time are the people who get GPU time.

**Earning & spending**

- Every frame one of **your workers renders** — for any node in the hive — earns **+1 🍯**, banked with your own coordinator.
- Every finished frame of **a job you posted** costs **−1 🍯**.
- Rendering your own job on your own GPU earns back exactly what it costs (**net zero**), so a solo node never runs dry.
- New nodes start with a **100 🍯 welcome stipend**. At **0 🍯 you cannot post new jobs** until you render frames for others — already-queued jobs keep rendering.

**Loans from the hive bank**

Need to render *right now* with an empty jar? Click the 🍯 stat in the dashboard to open
the **Honey Jar** and take a loan:

- Borrow **any amount** — no cap.
- **50% interest**: borrow N, repay **1.5×N** within **7 days**.
- Miss the deadline and the interest **doubles to 100%** (owe 2×N) and job posting
  freezes until the loan is repaid — frames your workers render still earn honey,
  so you can always work your way out of debt.
- One loan at a time; repay from your balance with one click (partial repayments via the API).

The dashboard shows your live balance, lifetime earned/spent, per-worker honey
leaderboard tags, and loan status (with a red **OVERDUE** warning when the bank
freezes your jar).

> Honey is a fun fairness layer for *trusted* LANs — RenderHive has no authentication,
> so it's honor-system accounting, not a cryptocurrency.

---

## Quick Start

**Requirements (every PC):** Windows, [Python 3.9+](https://www.python.org/downloads/) on PATH,
[Blender 4.0+](https://www.blender.org/download/) (5.x supported, auto-detected), same LAN.

1. Copy this folder to each PC (or clone the repo).
2. Double-click **`start_renderhive.bat`** on every PC.
   It creates a virtual environment, installs the two dependencies, detects your NVIDIA
   GPUs, adds the Windows Firewall rules (UDP 5678 + TCP 8080), and starts the node.
3. Open the dashboard URL it prints (e.g. `http://192.168.1.10:8080`) in any browser.
   Other PCs appear in the network mesh within ~15 seconds.

That's it — every PC is now simultaneously a render node and a submission point.

> **Peers not appearing?** Make sure the network profile is **Private** on every PC and
> that inbound UDP 5678 / TCP 8080 are allowed for Python (the launcher tries to add
> these rules; run it as Administrator if it couldn't).

Prefer the command line, or want options?

```
python renderhive.py                 # auto-detect everything
python renderhive.py --device CUDA   # Cycles backend: OPTIX (default) / CUDA / HIP / ONEAPI / METAL / CPU
python renderhive.py --gpus 0,1      # use specific GPUs
python renderhive.py --no-worker     # coordinator + dashboard only (no rendering)
python renderhive.py --port 9090     # custom dashboard port
```

---

## Submitting a Render Job

1. Click **＋ New Render Job** on the dashboard.
2. Pick a `.blend` (upload) **or** enter its full path (Shared Path mode — required for
   projects with relative asset paths, recommended with dependency packing).
3. Frame range, engine, samples and output format are **auto-detected from the .blend** —
   override anything you like. Choose a priority and an output folder.
4. Click **Queue Job** and watch the nodes light up. Download finished frames
   individually, or grab the whole sequence as a ZIP.
5. Make a video from the frames:
   ```
   ffmpeg -framerate 24 -i myjob_%04d.png -c:v libx264 -pix_fmt yuv420p out.mp4
   ```

---

## Dependency Packing — no more pink textures

Blender's *Pack Resources* can't pack videos, image sequences, movie clips, VDB volumes
or Alembic caches — on a render farm those turn into **pink textures** and missing
simulations. RenderHive fixes this the way Premiere's *Collect Files* does:

Leave **"Pack dependencies"** checked when submitting. The coordinator opens the .blend
in headless Blender, collects **every** external reference —

> videos used as textures · image sequences & UDIM tiles · movie clips · VSE strips ·
> audio · **VDB volumes** (single files *and* sequences) · **Alembic/USD caches**
> (Mesh Sequence Cache & Transform Cache) · fluid sim caches · linked libraries · fonts

— into an `assets/` folder beside a relinked copy of the blend, and ships that
self-contained project to every worker. Video frame-start offsets and playback settings
are preserved through the relink.

**Important:** Blender stores asset paths *relative to the .blend* by default, and
relative paths only resolve at the blend's original location — so submit packed jobs in
**Shared Path mode** with the blend's full path on the submitting PC
(e.g. `D:\projects\shot10\scene.blend`; it does *not* need to be a network share).
If any dependency is missing, the job **fails with the list of missing files** —
nothing silently renders pink — and *🔄 Retry Failed* re-attempts the packing.

Alternatives: pack textures into the .blend manually (`File → External Data → Pack
Resources` — fine for images/HDRIs only), or put the project on a NAS path mapped
identically on every PC and submit by Shared Path with packing off.

---

## EXR & TIFF Previews in the Browser

Browsers can't decode OpenEXR or TIFF, so render farms usually show broken thumbnails
for film-quality output. RenderHive converts finished EXR/TIFF frames to **cached JPEG
previews using headless Blender** (which reads every EXR flavor it can write — half/float,
ZIP/PIZ/DWAA codecs, multilayer) with proper linear → sRGB display transform.
Previews generate in the background the moment a frame is uploaded, thumbnails are
capped at 1280 px, and clicking a thumbnail downloads the **original** EXR/TIFF file.
PNG/JPEG/WebP jobs are served directly with zero overhead.

---

## How It Works (Architecture)

```
   PC A                          PC B                          PC C
┌──────────────────┐        ┌──────────────────┐        ┌──────────────────┐
│ renderhive.py    │  UDP   │ renderhive.py    │  UDP   │ renderhive.py    │
│  ├ discovery     │◄──────►│  ├ discovery     │◄──────►│  ├ discovery     │
│  ├ coordinator   │        │  ├ coordinator   │        │  ├ coordinator   │
│  │  (Flask +     │  HTTP  │  │  (dashboard,  │  HTTP  │  │  (jobs, 🍯,   │
│  │   dashboard)  │◄──────►│  │   jobs, 🍯)   │◄──────►│  │   frames)     │
│  └ worker ×GPU   │        │  └ worker ×GPU   │        │  └ worker ×GPU   │
└──────────────────┘        └──────────────────┘        └──────────────────┘
```

- **`renderhive.py`** — unified entry point: starts discovery, the coordinator, and one worker thread per detected GPU.
- **`coordinator.py`** — Flask HTTP API + dashboard. Owns the job queue, frame states, the honey ledger, ZIP/preview serving, and crash-recovery reapers.
- **`worker.py`** — pulls frames from *every* discovered coordinator, renders via headless Blender with live progress parsing, uploads results, and banks earned honey with its home node.
- **`discovery.py`** — UDP broadcast peer discovery (port 5678).
- **`pack_deps.py` / `preview_frame.py`** — run *inside* Blender for dependency collection and EXR→JPEG preview conversion.

All node data lives in `Documents\RenderHive\` (`blends/`, `output/<job>/`, `previews/`,
`state.json`, `settings.json`, `config.json`). Override the location with the
`RENDERHIVE_DATA_DIR` environment variable. The coordinator has a 200+ case pytest suite.

---

## Command Reference

Standalone pieces, if you don't want the all-in-one node:

```
python coordinator.py --port 8080          # coordinator + dashboard only

python worker.py [--server URL]            # explicit coordinator (else auto-discovery)
                 [--gpu N] [--device OPTIX|CUDA|HIP|ONEAPI|METAL|CPU]
                 [--name NAME] [--blender PATH] [--shared-root PATH]
```

Key HTTP endpoints (everything the dashboard does is plain JSON over HTTP):

| Endpoint | Purpose |
|---|---|
| `POST /api/jobs` | submit a job (402 when out of honey / loan overdue) |
| `GET /api/status` | farm state: nodes, jobs, peers, honey |
| `GET /api/jobs/<id>/zip` | download all finished frames |
| `GET /api/honey` · `POST /api/honey/loan` · `POST /api/honey/repay` | the hive bank |

---

## Troubleshooting & FAQ

**Workers/peers don't appear on the dashboard** → Windows Firewall. Allow Python on
private networks (UDP 5678, TCP 8080) and set the network profile to *Private* on every PC.

**"could not find Blender"** → pass `--blender "C:\Program Files\Blender Foundation\Blender 4.2\blender.exe"` (any 4.x/5.x).

**Pink textures / wrong-looking frames** → assets didn't travel. Re-submit with
**Pack dependencies** checked from a PC that can see the asset files, or use a shared
folder mounted at the same path everywhere.

**"Make the paths absolute" / packing fails on an uploaded blend** → the project uses
relative paths, which can't resolve away from their original folder. Submit by
**Shared Path** instead, or make paths absolute in Blender (`File → External Data`).

**"Out of honey" when posting a job** → your jar is empty: leave RenderHive running so
your GPUs earn honey rendering for the hive, or take a loan from the 🍯 panel
(50% interest, 7 days — don't be late, it doubles).

**OptiX errors on an older driver** → update the NVIDIA driver or use `--device CUDA`.

**A frame keeps failing** → after 3 attempts it's marked failed (the per-frame Blender
log is kept on the coordinator); other frames continue. *🔄 Retry Failed* requeues them.

**Will this speed up a single still image?** → No — distributed rendering parallelizes
*frames*, not pixels of one frame. For stills, use your fastest single GPU.

**Can I make a double-click .exe for workers?** →
`pip install pyinstaller && pyinstaller --onefile worker.py`, then distribute `dist/worker.exe`.

---

Made with 🍯 by **FireDrum**. If RenderHive saved your render night, star the repo —
it helps other Blender artists find it.
