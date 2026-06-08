#!/usr/bin/env python3
"""
RenderHive - Peer Discovery
============================
UDP broadcast-based peer discovery for automatic LAN detection.
Every node broadcasts its presence and listens for other nodes.
"""

import socket
import json
import threading
import time

DEFAULT_DISCOVERY_PORT = 5678
BROADCAST_INTERVAL = 5
PEER_TIMEOUT = 30
MAGIC = "RENDERHIVE"


class PeerDiscovery:
    """Manages UDP broadcast-based peer discovery."""

    def __init__(self, node_id, http_port, node_name=None,
                 discovery_port=DEFAULT_DISCOVERY_PORT):
        self.node_id = node_id
        self.http_port = http_port
        self.node_name = node_name or socket.gethostname()
        self.discovery_port = discovery_port
        self.peers = {}       # node_id -> peer info
        self.lock = threading.Lock()
        self._running = False
        self._wake_event = threading.Event()
        self._local_ip = self._detect_lan_ip()

    def _detect_lan_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    @property
    def local_ip(self):
        return self._local_ip

    def start(self):
        self._running = True
        threading.Thread(target=self._broadcast_loop, daemon=True,
                         name="discovery-broadcast").start()
        threading.Thread(target=self._listen_loop, daemon=True,
                         name="discovery-listen").start()
        threading.Thread(target=self._cleanup_loop, daemon=True,
                         name="discovery-cleanup").start()

    def stop(self):
        self._running = False

    def get_peers(self):
        with self.lock:
            return list(self.peers.values())

    def get_all_coordinator_urls(self):
        """Return HTTP URLs for all known nodes (self + peers)."""
        urls = [f"http://127.0.0.1:{self.http_port}"]
        with self.lock:
            for p in self.peers.values():
                urls.append(f"http://{p['ip']}:{p['port']}")
        return urls

    def broadcast_wake(self):
        """Send wake signal so all workers immediately poll for work."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            msg = json.dumps({"magic": MAGIC, "type": "wake",
                              "node_id": self.node_id}).encode("utf-8")
            sock.sendto(msg, ("<broadcast>", self.discovery_port))
            sock.close()
        except Exception:
            pass

    def wait_for_wake(self, timeout=None):
        """Block until a wake signal is received. Returns True if woken."""
        result = self._wake_event.wait(timeout=timeout)
        self._wake_event.clear()
        return result

    def _broadcast_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        msg_data = {
            "magic": MAGIC, "type": "announce",
            "node_id": self.node_id, "port": self.http_port,
            "name": self.node_name,
        }
        while self._running:
            try:
                sock.sendto(json.dumps(msg_data).encode("utf-8"),
                            ("<broadcast>", self.discovery_port))
            except Exception:
                pass
            time.sleep(BROADCAST_INTERVAL)
        sock.close()

    def _listen_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", self.discovery_port))
        except OSError as e:
            print(f"  discovery: could not bind UDP port {self.discovery_port}: {e}")
            return
        sock.settimeout(2.0)
        while self._running:
            try:
                data, addr = sock.recvfrom(4096)
                msg = json.loads(data.decode("utf-8"))
                if msg.get("magic") != MAGIC:
                    continue
                if msg["type"] == "announce" and msg["node_id"] != self.node_id:
                    self._handle_announce(msg, addr[0])
                elif msg["type"] == "wake" and msg["node_id"] != self.node_id:
                    self._wake_event.set()
            except socket.timeout:
                continue
            except Exception:
                pass
        sock.close()

    def _handle_announce(self, msg, ip):
        nid = msg["node_id"]
        with self.lock:
            is_new = nid not in self.peers
            self.peers[nid] = {
                "node_id": nid, "ip": ip, "port": msg["port"],
                "name": msg["name"], "last_seen": time.time(),
            }
        if is_new:
            print(f"  discovery: found peer '{msg['name']}' at {ip}:{msg['port']}")

    def _cleanup_loop(self):
        while self._running:
            time.sleep(10)
            now = time.time()
            with self.lock:
                dead = [nid for nid, p in self.peers.items()
                        if now - p["last_seen"] > PEER_TIMEOUT]
                for nid in dead:
                    p = self.peers.pop(nid)
                    print(f"  discovery: peer '{p['name']}' went offline")
