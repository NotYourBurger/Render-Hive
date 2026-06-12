#!/usr/bin/env python3
"""
RenderHive - Unified Entry Point
=================================
Single script that runs on EVERY PC in the render farm.
- Starts peer discovery (UDP broadcast)
- Starts the coordinator (Flask HTTP API + dashboard)
- Spawns one worker per GPU automatically
- No configuration needed — just run it!

Usage:
    python renderhive.py                    # auto-detect everything
    python renderhive.py --port 8080        # custom HTTP port
    python renderhive.py --device CUDA      # use CUDA instead of OPTIX
    python renderhive.py --gpus 0,1         # specific GPUs only
    python renderhive.py --no-worker        # coordinator only (no rendering)
    python renderhive.py --workers 0        # same as --no-worker
"""

import os
import sys
import json
import uuid
import time
import socket
import argparse
import threading
from pathlib import Path

# Ensure the script's directory is on the path for imports
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from discovery import PeerDiscovery
from coordinator import create_app, detect_blender, DATA_DIR
from worker import Worker, detect_gpu_count, detect_gpu_name

CONFIG_FILE = DATA_DIR / "config.json"


def load_config():
    """Load persisted config (node_id, node_name)."""
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_config(cfg):
    """Save config to disk."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def get_node_name(args, cfg):
    """Determine the node name. Prompt user if no name is configured."""
    # 1. CLI flag overrides everything
    if args.name:
        return args.name

    # 2. Saved config name
    if cfg.get("node_name"):
        return cfg["node_name"]

    # 3. First launch — ask the user
    hostname = socket.gethostname()
    print()
    print("=" * 60)
    print("  Welcome to RenderHive!")
    print("  No PC name configured yet.")
    print(f"  (Default hostname: {hostname})")
    print("=" * 60)
    print()
    try:
        name = input("  Enter a name for this PC (e.g. 'Workstation-A'): ").strip()
    except (EOFError, KeyboardInterrupt):
        name = ""
    if not name:
        name = hostname
        print(f"  Using default: {name}")
    else:
        print(f"  Saved: {name}")

    # Save it
    cfg["node_name"] = name
    save_config(cfg)
    return name


def get_node_id(cfg):
    """Get or generate a persistent node ID."""
    if cfg.get("node_id"):
        return cfg["node_id"]
    nid = uuid.uuid4().hex[:12]
    cfg["node_id"] = nid
    save_config(cfg)
    return nid


def parse_args():
    ap = argparse.ArgumentParser(
        description="RenderHive — peer-to-peer distributed Blender render farm",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python renderhive.py                  Run with auto-detected settings
  python renderhive.py --port 9090      Use port 9090 for HTTP
  python renderhive.py --device CUDA    Use CUDA backend for Cycles
  python renderhive.py --no-worker      Run coordinator only (no GPU rendering)
  python renderhive.py --gpus 0,1       Use only GPUs 0 and 1
        """)
    ap.add_argument("--port", type=int, default=8080,
                    help="HTTP port for the coordinator dashboard (default: 8080)")
    ap.add_argument("--host", default="0.0.0.0",
                    help="HTTP bind address (default: 0.0.0.0)")
    ap.add_argument("--discovery-port", type=int, default=5678,
                    help="UDP port for peer discovery (default: 5678)")
    ap.add_argument("--device", default="OPTIX",
                    choices=["OPTIX", "CUDA", "HIP", "ONEAPI", "METAL", "CPU"],
                    help="Cycles GPU backend (default: OPTIX)")
    ap.add_argument("--gpus", default=None,
                    help="Comma-separated GPU indices to use (default: all detected)")
    ap.add_argument("--no-worker", action="store_true",
                    help="Don't start any worker threads (coordinator only)")
    ap.add_argument("--workers", type=int, default=None,
                    help="Number of worker threads to start (0 = no workers)")
    ap.add_argument("--blender", default=None,
                    help="Path to blender executable")
    ap.add_argument("--shared-root", default=None,
                    help="Local mount point of shared project folder")
    ap.add_argument("--name", default=None,
                    help="Node name (default: hostname)")
    return ap.parse_args()


def get_gpu_indices(args):
    """Determine which GPU indices to use."""
    if args.no_worker or args.workers == 0:
        return []
    if args.gpus:
        return [int(g.strip()) for g in args.gpus.split(",")]
    count = detect_gpu_count()
    if args.workers is not None:
        count = min(count, args.workers)
    return list(range(count))


def start_worker_thread(gpu_index, device, blender, shared_root,
                        discovery, name_prefix=None, home_server=None):
    """Start a worker in a daemon thread."""
    name = f"{name_prefix or socket.gethostname()}-gpu{gpu_index}"
    w = Worker(
        gpu_index=gpu_index,
        device=device,
        blender=blender,
        shared_root=shared_root,
        name=name,
        discovery=discovery,
        server=None,  # use discovery
        home_server=home_server,  # honey earned anywhere is banked here
    )
    t = threading.Thread(target=w.run, daemon=True,
                         name=f"worker-gpu{gpu_index}")
    t.start()
    return t


def main():
    args = parse_args()

    # Load or create config
    cfg = load_config()

    # Get persistent node ID and name
    node_id = get_node_id(cfg)
    node_name = get_node_name(args, cfg)

    # Detect Blender
    blender = args.blender or detect_blender()

    # Determine GPU indices
    gpu_indices = get_gpu_indices(args)

    # Print banner
    print()
    print("=" * 60)
    print("  ╦═╗┌─┐┌┐┌┌┬┐┌─┐┬─┐╦ ╦┬┬  ┬┌─┐")
    print("  ╠╦╝├┤ │││ ││├┤ ├┬┘╠═╣│└┐┌┘├┤ ")
    print("  ╩╚═└─┘┘└┘─┴┘└─┘┴└─╩ ╩┴ └┘ └─┘")
    print("  Peer-to-Peer Distributed Render Farm")
    print("=" * 60)

    # Detect LAN IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan_ip = s.getsockname()[0]
        s.close()
    except Exception:
        lan_ip = "127.0.0.1"

    print(f"  Node:       {node_name} ({node_id})")
    print(f"  LAN IP:     {lan_ip}")
    print(f"  Dashboard:  http://{lan_ip}:{args.port}")
    print(f"  Discovery:  UDP port {args.discovery_port}")
    print(f"  User data:  {DATA_DIR}")

    if blender:
        print(f"  Blender:    {blender}")
    else:
        print("  Blender:    NOT FOUND (workers disabled)")
        gpu_indices = []

    if gpu_indices:
        gpu_names = [f"GPU {i}: {detect_gpu_name(i)}" for i in gpu_indices]
        print(f"  GPUs:       {', '.join(gpu_names)}")
        print(f"  Device:     {args.device}")
    else:
        print("  GPUs:       none (coordinator only)")

    print("=" * 60)
    print()

    # 1. Start peer discovery
    discovery = PeerDiscovery(
        node_id=node_id,
        http_port=args.port,
        node_name=node_name,
        discovery_port=args.discovery_port,
    )
    discovery.start()
    print("  [✓] Peer discovery started")
    print(f"      If other PCs don't appear within ~15s, allow inbound "
          f"UDP {args.discovery_port} and TCP {args.port} for Python in "
          f"Windows Firewall, and make sure the network profile is "
          f"'Private' on every PC.")

    # 2. Create and configure the coordinator Flask app
    flask_app = create_app(discovery=discovery, blender_path=blender)
    print("  [✓] Coordinator initialized")

    # 3. Start worker threads (one per GPU)
    worker_threads = []
    if gpu_indices and blender:
        # Small delay to let the Flask server start first
        def start_workers_delayed():
            time.sleep(2)  # wait for Flask to be ready
            for gi in gpu_indices:
                t = start_worker_thread(
                    gpu_index=gi,
                    device=args.device,
                    blender=blender,
                    shared_root=args.shared_root,
                    discovery=discovery,
                    name_prefix=node_name,
                    home_server=f"http://127.0.0.1:{args.port}",
                )
                worker_threads.append(t)
                print(f"  [✓] Worker GPU {gi} started")

        threading.Thread(target=start_workers_delayed, daemon=True,
                         name="worker-launcher").start()

    # 4. Run Flask (blocking)
    print(f"  [✓] Starting HTTP server on {args.host}:{args.port}")
    print()
    try:
        flask_app.run(host=args.host, port=args.port, threaded=True)
    except KeyboardInterrupt:
        print("\n  Shutting down...")
        discovery.stop()
        print("  Goodbye!")


if __name__ == "__main__":
    main()
