# RenderHive Improvement Plan

**North star:** evolve RenderHive from a trusted-LAN render farm into a **public, decentralized
render network that works like BitTorrent** — no central operator, no blockchain, no crypto
wallet. BitTorrent proved you can get global decentralization with three ingredients we can
copy directly:

| BitTorrent | RenderHive equivalent |
|---|---|
| Infohash / content addressing | Hash-addressed blends, assets, and rendered frames |
| Tracker → DHT (Kademlia) | Bootstrap node → DHT for peer discovery beyond the LAN |
| Tit-for-tat choking / ratio economies | Honey, upgraded from honor-system to signed per-pair accounting |
| Swarm piece exchange | Workers seeding packed assets to each other |

What renderhive.io promises with Hashgraph + IPFS + smart contracts, we can deliver with
plain cryptography and P2P protocols — the same way private torrent trackers run ratio
economies without a chain.

**Explicit non-goals:** blockchain, tokens, payments in money, NFT/copyright registries.
Honey stays a reciprocity currency, like upload ratio.

---

## Competitor teardown — lessons from renderhive.io's Go backend

A read of their backend source (see `BACKEND_ARCHITECTURE.md`) confirms the status
assessment: the **data plane works** (embedded IPFS/kubo node, pin/fetch by CID,
Filecoin persistence via w3up) and the **control plane works** (Hedera HCS topics as an
ordered message log), but everything in between — job matching, job execution wiring,
result submission, validation, payment — is a `TODO` stub. They built the
infrastructure first and the render farm last; we built the render farm first and are
adding infrastructure. Our sequencing is the right one, but their infrastructure
choices are worth studying.

**Patterns to adopt (referenced in the phases below):**

1. **Strict data-plane / control-plane separation.** Bulk data (blends, assets,
   results) moves content-addressed peer-to-peer; control messages are tiny signed
   JSON envelopes that carry only *hashes*. We should keep this discipline as Phase 3/4
   land: the job/honey/offer protocol never carries file bytes, the transfer protocol
   never carries decisions.
2. **Deploy ≠ submit.** They publish data to the network first ("available to anyone
   who knows the CID") and announce the CID second. Adopt for Phase 4: a job's assets
   are seeded before the job goes visible, so the first worker never stalls on a
   half-uploaded project.
3. **Benchmark-normalized pricing unit.** Their "BBP" is the official Blender
   benchmark's samples/min score, measured per node per device. This is the concrete
   answer to how Phase 2 should weight honey — don't invent a unit, use Blender
   OpenData's.
4. **Hash-verified, network-distributed Blender builds.** They pin official Blender
   archives content-addressed so every render node runs a *bit-identical* binary. This
   matters to us more than it seems: Phase 2's spot-check verification only works if
   two nodes rendering the same frame produce comparable output, and that starts with
   identical Blender builds.
5. **Announce public addresses, multiple transports.** Their node appends its public
   IPv4/IPv6 to its announced addresses (TCP + QUIC + WebTransport) so home-router
   nodes stay dialable — a checklist item for Phase 3 NAT traversal.
6. **State from replayable logs.** Nodes reconstruct network state by replaying the
   full topic history. Our signed honey receipts (Phase 1) should likewise be an
   append-only replayable log per node, so state is auditable and rebuildable —
   without needing a global ledger.

**Mistakes to avoid:**

- **Every node pins everything.** Their job-queue callback makes *every* node fetch
  and pin *every* announced blend (their own comment admits it won't scale). Phase 4
  replicates on demand: only assigned workers fetch a job, plus a small k-replication
  factor for durability.
- **Coordination cathedral before a working farm.** Globally synchronized "hive
  cycles" computed from consensus time, four message topics, a smart-contract escrow —
  all to support a matching algorithm that doesn't exist yet. Our pull-based model
  (worker asks, coordinator answers) needs no global clock and no phases; keep it that
  way as long as possible.

**One real problem their fees solve that we must solve differently:** every HCS message
costs money, which is built-in spam and sybil resistance. A free network has neither.
Our answer is layered: signed identities (Phase 1), per-pair tit-for-tat throttling
(Phase 2), and reputation-gated service for new keys (Phase 5) — plus rate limits at
every endpoint. This needs to be designed deliberately, not bolted on.

---

## Current state (June 2026)

Working LAN farm: `renderhive.py` (unified node), `coordinator.py` (~1,700 lines, Flask API +
dashboard + honey ledger), `worker.py` (~600 lines, pull-based), `discovery.py` (UDP broadcast),
`pack_deps.py` (dependency collection), 80 unit tests for the coordinator.

Gaps that block the north star, confirmed in code:

- **No integrity checks** — zero sha256/checksum on any blend/asset/frame transfer
  (only an md5 path-tag in `pack_deps.py`).
- **No identity or auth** — every endpoint is open; `POST /api/honey/earn` lets anyone
  mint honey; worker IDs are self-asserted strings.
- **Flat economy** — 1 honey/frame regardless of frame cost or GPU; priority is a separate
  dropdown instead of emerging from price.
- **LAN-only** — UDP broadcast discovery, plain HTTP, no NAT traversal.
- **Windows-only** — launcher, firewall setup, and path handling assume Windows.
- **Untrusted-input hazard** — rendering a stranger's .blend is arbitrary code execution
  (drivers, auto-run scripts); fine on a LAN, disqualifying for a public network.

---

## Phase 0 — Foundation hardening (LAN, no behavior change)

*Goal: everything later builds on integrity + portability. Ship in current releases.*

1. **Content hashing on every transfer.** sha256 each blend, packed `assets/` file, and
   uploaded frame. Coordinator stores hashes in `state.json`; worker verifies after download
   (`/api/jobs/<id>/blend`) and coordinator verifies after upload (`/api/workers/<wid>/complete`).
   Corrupt transfer → automatic refetch, not a garbage render. This *is* content addressing —
   the hash becomes the file's identity in Phase 4.
2. **Cross-platform support.** Replace Windows-only assumptions in `renderhive.py` and the
   `.bat` launchers: Blender auto-detect on Linux/macOS, a `start_renderhive.sh`,
   `pathlib` everywhere, GPU detection beyond `nvidia-smi` where feasible.
3. **Packaging.** `pyproject.toml`, `pip install renderhive`, `renderhive` console entry point.
   A public network needs frictionless install more than any feature.
4. **Test the worker and discovery.** The coordinator has 80 tests; `worker.py` and
   `discovery.py` have none. Add download/verify/render-loop tests with a mocked Blender.

## Phase 1 — Identity & a forgery-proof ledger

*Goal: honey survives untrusted participants; prerequisite for any WAN exposure.*

1. **Node keypairs.** Generate an Ed25519 keypair on first run (`config.json`); the public
   key is the permanent node ID (like a BitTorrent peer ID, but verifiable). Display names
   stay cosmetic.
2. **Signed honey entries.** Replace the open `POST /api/honey/earn` with a signed receipt
   chain: when coordinator C accepts a verified frame from worker W, C signs
   `{job, frame, result_hash, worker_pubkey, timestamp}`; W banks the receipt with its home
   node, which verifies C's signature. Nobody can mint honey without a counterparty signature.
   Store receipts as an **append-only, replayable log** (the balance is a fold over it, like
   the competitor reconstructing state by replaying topic history) — auditable, rebuildable
   after corruption, and disputes reduce to "show me the receipts."
3. **Request signing + shared-secret fallback.** Sign worker→coordinator API calls with the
   node key. For dashboards, an optional API token — the minimal step that lets people safely
   port-forward a node today.
4. **TLS.** Self-signed cert pinned by node ID (BitTorrent-style: identity = key, no CA needed).

## Phase 2 — A real economy (price discovery, fair earning)

*Goal: take their best idea — the order book — without their blockchain.*

1. **Cost-weighted honey.** Earn honey proportional to measured render time × a node
   benchmark factor, not 1/frame. The coordinator already measures per-frame durations for
   ETAs — reuse that. For the benchmark factor, adopt the competitor's unit rather than
   inventing one: run the official Blender benchmark CLI (`benchmark-launcher-cli`) per
   device on first start, store the **samples/min** score in `config.json`, and include
   it in worker registration. Honey per frame ≈ render seconds × node score, normalized
   so today's "1 honey ≈ 1 average frame" intuition survives.
2. **Price-as-priority.** Submitters attach a honey bid per frame; workers (configurably)
   prefer higher-paying frames. The Low→Urgent dropdown becomes a bid preset. This replicates
   renderhive.io's order-book matching with zero new infrastructure — the existing pull
   endpoint (`/api/workers/<wid>/next`) just sorts by bid. Their offer/request document
   schema is a good template for what a worker's standing "offer" should declare: supported
   Blender versions, engines (EEVEE/Cycles), devices, and a minimum honey rate — which
   becomes capability matching (don't hand an OPTIX-only job to a CPU node) as well as
   price matching.
3. **Spot-check verification.** Re-render a small random sample of frames (or low-sample
   thumbnails of them) on a second node and compare perceptually; nodes that fail forfeit the
   honey and lose reputation. This is the no-blockchain answer to their smart-contract escrow.
   **Prerequisite — render determinism:** comparisons are only meaningful when both nodes run
   a bit-identical Blender build, so jobs pin an exact Blender version+hash (the manifest
   carries it), workers verify their binary's sha256 against a published manifest of official
   Blender builds, and mismatched nodes skip the job. (The competitor distributes
   hash-verified Blender archives over the network for exactly this reason; Phase 4's
   content store can carry Blender archives the same way.) Even then GPU output isn't
   bit-exact across vendors — compare perceptually (SSIM-style threshold), not by hash.
4. **Per-pair tit-for-tat.** Track honey balances per peer pair in addition to the global
   jar, so node A throttles work for node B if B never reciprocates — BitTorrent's choking
   algorithm applied to render frames. Keeps the public network healthy without global consensus.

## Phase 3 — Beyond the LAN

*Goal: two nodes on different networks form a hive.*

1. **Manual peering first.** `renderhive.py --peer host:port` with signed announcements —
   friends-and-family WAN, the simplest useful step.
2. **Bootstrap nodes (tracker stage).** A tiny public endpoint that introduces peers, exactly
   like a torrent tracker. Run one; let anyone run their own.
3. **DHT (trackerless stage).** Kademlia keyed by node ID for discovery; LAN UDP broadcast
   remains the zero-config fast path (BitTorrent kept local peer discovery too).
   **Build-vs-buy:** the competitor embeds a full IPFS/libp2p node and gets DHT, NAT
   traversal, and multi-transport for free — but in Go, where libp2p is first-class.
   In Python the honest options are (a) shell out to a bundled kubo/IPFS daemon for the
   data plane, (b) a minimal own Kademlia (it's ~500 lines for our needs), or (c) port
   the networking core to a compiled sidecar later. Decide at the start of this phase;
   don't half-adopt.
4. **NAT traversal.** UPnP/NAT-PMP port mapping, UDP hole punching with the bootstrap node
   as introducer, relay fallback for symmetric NATs. Announce the node's *public*
   IPv4/IPv6 alongside LAN addresses in every peer announcement (the competitor does this
   explicitly), and prefer QUIC/UDP where available since it traverses home NATs better
   than TCP.
5. **Keep control and data planes separate.** As WAN messaging lands, enforce the
   competitor's one good architectural rule: control messages (job announcements, bids,
   honey receipts, peer gossip) are small signed JSON carrying only hashes; file bytes
   move exclusively over the Phase 4 transfer protocol. No blends in control messages,
   no decisions in the data path.

## Phase 4 — Swarm content distribution

*Goal: assets move like torrent pieces, not coordinator downloads.*

1. **Content-addressed store.** Every packed file is stored and requested by its sha256
   (Phase 0 hashes become addresses). A job manifest is a list of hashes — effectively a
   .torrent file for a render job. The manifest also pins the required Blender
   version+hash (see Phase 2 determinism), and the store can distribute hash-verified
   Blender archives themselves, the way the competitor does.
2. **Deploy, then announce.** Seed a job's full content (locally + to at least one other
   peer) *before* it becomes visible in the queue — the competitor's two-step
   deploy/submit pattern. Workers never claim a frame whose assets aren't yet fully
   retrievable, and a submitter going offline mid-upload can't strand a job.
3. **Peer seeding, on-demand replication.** Workers fetch pieces from any peer that has
   them, not just the originating coordinator; the second worker on a job downloads mostly
   from the first. Kills the coordinator-bandwidth bottleneck on large VDB/Alembic jobs.
   Explicitly do **not** copy the competitor's replicate-on-announce (every node pins every
   announced job — their own code comments admit it won't scale): only assigned workers
   fetch, plus a small configurable k-replication for durability so the job survives the
   submitter going offline. That k-replication is our no-Filecoin answer to their w3up
   persistence layer.
4. **Dedup cache.** Content addressing makes re-submits of the same project nearly free —
   workers already have the unchanged assets.

## Phase 5 — Public-network safety (the hard, non-optional part)

*Rendering a stranger's .blend executes their code. This phase gates any public launch.*

1. **Sandboxed Blender.** Run renders with scripting disabled (`--disable-autoexec` is not
   enough — audit drivers/expressions) inside an OS sandbox: container/namespaces on Linux,
   AppContainer or a VM on Windows. No network, no filesystem outside the job dir.
2. **Resource limits.** Wall-clock, VRAM, disk quotas per job; kill and requeue on breach.
3. **Reputation, not just honey.** Long-lived node keys accumulate verified-frame history;
   new keys start throttled (sybil resistance the BitTorrent way: cheap identities get
   bottom-of-the-barrel service, reputation is earned).
4. **Operator controls & legal reality.** Public means strangers' content renders on your
   GPU. Allowlist/blocklist by node key, "friends-of-friends only" mode, no-VSE/no-video
   policy toggles, and clear terms — SheepIt's moderation experience is the cautionary tale
   to study.

## Phase 6 — Ecosystem & polish

1. **Blender add-on** — submit the open scene to the hive from inside Blender (their site
   doesn't promise this; SheepIt's add-on is why people use it).
2. **Public landing page with live (real) stats** — they show fake numbers; we can show real
   ones from day one.
3. **Installers** — signed `.exe`/`.dmg`/AppImage so node operators never see Python.
4. **Docs split** — operator guide vs. artist guide vs. protocol spec (the protocol spec is
   what invites other implementations, like BEP documents did for BitTorrent).

---

## Sequencing & effort

| Phase | Depends on | Rough size | Unlocks |
|---|---|---|---|
| 0 Hashing/portability | — | small | integrity, Linux nodes |
| 1 Identity/ledger | 0 | medium | safe port-forwarding, honest honey |
| 2 Economy | 1 | medium | fair earning, bid priority |
| 3 WAN discovery | 1 | large | internet hives |
| 4 Swarm transfer | 0, 3 | large | big-asset scalability |
| 5 Sandboxing/reputation | 1–3 | large | **public launch gate** |
| 6 Ecosystem | any | ongoing | adoption |

Phases 0–2 are pure wins for the existing LAN product even if the public network never ships.
Phase 5 is the honest blocker for "public BitTorrent-style": until renders are sandboxed and
identities are rate-limited, the network must stay invite/LAN scoped.
