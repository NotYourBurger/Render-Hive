# Renderhive Backend — Networking & Render Distribution Architecture

This document describes how the Go backend (`backend/src/`) implements peer-to-peer
networking, how Blender files are shared across peers, how render jobs are distributed,
and how render data is uploaded and downloaded.

> **Status note:** The backend is a work in progress. The data-sharing and coordination
> layers (IPFS, Hedera HCS, w3up) are functional, while parts of the job distribution
> logic (job claiming, mediator nodes, result submission, payment via smart contract)
> are still stubs marked `TODO` in the code.

---

## 1. High-Level Architecture

The backend is a single Go service composed of package "managers", initialized in this
order by `AppManager.Init()` (`backend/src/app.go`):

| Manager | Package | Responsibility |
|---|---|---|
| `hedera.Manager` | `hedera/` | Hedera Hashgraph client, Consensus Service (HCS) topics, mirror node, smart contract calls |
| `ipfs.Manager` | `ipfs/` | Embedded IPFS node (kubo), file add/get/pin, w3up (web3.storage/Filecoin) agent |
| `node.Manager` | `node/` | Node identity, render offers/requests, hive cycle, Blender process control |
| `jsonrpc.Manager` | `jsonrpc/` | Local JSON-RPC API consumed by the web frontend |
| `webapp.Manager` | `webapp/` | Serves the bundled web UI |

There are two distinct "networks" the backend talks to, with a strict separation of concerns:

- **IPFS / libp2p** — the *data plane*. All bulk data (`.blend` files, render request
  documents, render offer documents, Blender binaries) is content-addressed and moved
  peer-to-peer over IPFS.
- **Hedera Consensus Service (HCS)** — the *control plane*. Nodes never send commands
  to each other directly. Instead they publish small JSON messages (containing IPFS CIDs)
  to shared HCS topics, which gives every node the same fairly-ordered, immutable,
  timestamped event log.

A node can act as a **client node** (submits render requests), a **render node**
(offers render power), or both (`NodeData` in `node/root.go`). Mediator nodes are
declared but not implemented.

---

## 2. Peer-to-Peer Networking

### 2.1 Embedded IPFS node

Each service app runs a **full embedded IPFS (kubo) node in-process** — there is no
external IPFS daemon. `ipfs.PackageManager.StartLocalNode()` (`ipfs/root.go`):

1. Creates/opens a local IPFS repository under the app data dir (`renderhive/ipfs/repo/`),
   generating a fresh 2048-bit identity key on first run. Experimental kubo features
   (filestore, urlstore, libp2p stream mounting, p2p HTTP proxy) are explicitly disabled.
2. Queries the machine's **public IPv4 and IPv6 addresses** and appends them to the
   node's announced multiaddrs (`Addresses.AppendAnnounce`) for TCP 4001, QUIC,
   QUIC-v1, and WebTransport — so peers behind home routers are still dialable.
3. Spawns the node with `Online: true` and `Routing: libp2p.DHTOption`, i.e. the node
   is a **full DHT node** that both fetches *and stores/serves* DHT records (rather
   than a client-only DHT node).
4. Blocks at startup until the node has bootstrapped to **at least 4 swarm peers**
   (fails after 10 s with zero peers).

Peer management is plain libp2p swarm management exposed through helpers and CLI
commands (`renderhive ipfs swarm connect|disconnect|peers`):

- `SwarmConnect(multiaddr)` / `SwarmDisconnect(multiaddr)` — manual peering.
- `GetConnectedPeers()` — lists current swarm connections.

An optional local HTTP server (`StartHTTPServer`) exposes the standard IPFS gateway
(`/ipfs`, `/ipns`) and WebUI on a configurable port for debugging/inspection.

**Discovery of content** works the standard IPFS way: a node that adds a file becomes
a DHT *provider* for its CID; any other node that learns the CID resolves providers
via the DHT and fetches blocks via Bitswap from whichever peers have them.

### 2.2 Hedera HCS as the coordination/messaging layer

Nodes coordinate through four **HCS topics** (testnet IDs in `globals/constants.go`):

| Topic | Purpose |
|---|---|
| Hive Cycle Synchronization (`0.0.2659511`) | Broadcasts hive-cycle configuration (iteration, duration) used by all nodes to compute the current cycle |
| Hive Cycle Application (`0.0.2659514`) | Nodes apply to participate in a cycle (callback currently a stub) |
| Hive Cycle Validation (`0.0.2659516`) | Reserved for result validation (callback currently a stub) |
| **Render Job Queue** (`0.0.2659518`) | The actual job market: render requests, render offers, cancellations |

After the operator signs in (handled in `jsonrpc/operators.go`), the backend subscribes
to all four topics from `time.Unix(0,0)` — i.e. it **replays the full topic history**
through Hedera mirror nodes to reconstruct network state, then keeps receiving live
messages via callbacks.

### 2.3 The Renderhive command protocol (JSON-RPC over HCS)

Defined in `node/commands.go`. Every network-visible action is a `RenderhiveCommand`:

```json
{
  "ver": "1.0",                 // protocol version
  "aud": [],                    // audience: node account IDs; empty = broadcast
  "rpc": "<base64 JSON-RPC>"    // embedded JSON-RPC 2.0 message
}
```

The embedded JSON-RPC message uses `Service.Method` names (e.g.
`NodeService.SubmitRenderRequest`, `NodeService.SubmitRenderOffer`,
`NodeService.CancelRenderRequest`, `NodeService.PauseRenderOffer`, plus a
`ContractService` family for smart-contract operations). Each node receives every
topic message, decodes it (`DecodeCommand` → base64 → JSON-RPC), and dispatches it
internally — effectively calling its own local JSON-RPC handlers with the network as
the transport.

Why HCS instead of direct libp2p messaging (per the comment in `commands.go`):

- messages are **immutable and fairly ordered** by Hedera consensus, preventing
  command collisions due to latency in a distributed network;
- the log is **auditable** by anyone;
- it is **spam-resistant**, since every message costs a network fee.

Transactions that change network state are not signed by the backend directly: the
backend builds and freezes the transaction, returns the **transaction bytes (hex)**
to the frontend via local JSON-RPC, and the operator's wallet signs and executes them
(see `SubmitRenderRequest` in `jsonrpc/node.go`).

### 2.4 Hive cycles (network heartbeat)

`node/hivecycles.go`. The hive operates in discrete time slices called **hive cycles**,
computed from Hedera **consensus time** rather than local clocks so that all nodes agree:

1. Configuration messages on the synchronization topic define cycle `duration` (and
   iteration) starting from their consensus timestamp.
2. A background goroutine (started in `app.go`) calls `HiveCycle.Synchronize()` every
   100 ms. It queries the mirror node for the latest transaction's consensus timestamp
   **at most once per hour** (`RENDERHIVE_CONFIG_HIVE_CYCLE_SYNCHRONIZATION_INTERVAL`),
   computes the local-vs-network clock offset, and extrapolates network time locally
   between syncs.
3. The current cycle number is `ceil((networkTime - configTimestamp) / duration)`
   accumulated over all configuration messages.
4. When the cycle number changes, the node enters the cycle's phases. Only the
   **application phase** is partially implemented (a busy node skips the cycle);
   distribution, render-contract, validation, and claiming phases are TODO stubs.

---

## 3. How Blender Files Are Shared Across Peers

### 3.1 Packaging a render request

A render request is created either via the local JSON-RPC API
(`NodeService.CreateRenderRequest`, used by the web frontend) or the CLI. The flow
(`jsonrpc/node.go` + `node/render.go`):

1. The frontend sends project files as **base64-encoded blobs**. The backend decodes
   them and wraps each one as an in-memory IPFS file object
   (`RenderRequest.AddFileFromBytes`). Exactly **one `.blend` file** is allowed per
   request; its CID is computed immediately (hash-only, without publishing).
2. All files are assembled into a single **IPFS map directory**
   (`files.NewMapDirectory` in `MakeDirectory`).
3. A **render request document** (JSON) is written locally to
   `renderhive/data/render_requests/local/request-<owner>-<timestamp>.json`. It
   contains the directory CID, Blender file CID + render settings, requested Blender
   version, max price (USD cents per BBP), timestamps, and the owner's Hedera account ID.

### 3.2 Deploy: publish to IPFS (data available, not yet announced)

`RenderRequest.Deploy()`:

- adds the project **directory** to the local IPFS node with **pinning enabled** →
  `DirectoryCID`;
- adds the **request document** the same way → `DocumentCID`.

At this point the data is technically retrievable by anyone *who knows the CID*, but
no one has been told the CID yet. The code explicitly notes this two-step design:
deploy = make available, submit = announce. (Encrypting the payload so only assigned
render nodes can read it is noted as a possible future improvement.)

The same pattern applies to **render offers** (`RenderOffer.AddDocument()` +
`Deploy()`): an offer document (supported Blender versions/engines/devices/threads,
price threshold, owner) is written to `renderhive/data/render_offers/local/` and
pinned to IPFS.

### 3.3 Submit: announce the CID on the job queue topic

`RenderRequest.Submit()` encodes a `NodeService.SubmitRenderRequest` command carrying
`{render_request_cid, blender_file_cid}` and submits it to the **Render Job Queue
topic** on Hedera (memo `renderhive-v0.1.0::submit-render-request`). From this moment
every subscribed node knows the CIDs.

### 3.4 Replication on the receiving side

Every node's `JobQueueMessageCallback()` (`node/render.go`) processes incoming job
queue messages:

- **`SubmitRenderRequest`** → the node immediately (in goroutines) **pins both the
  render request document and the `.blend` file** on its local IPFS node
  (`ipfs.Manager.PinObject`), then appends a `RenderJob` entry to its in-memory
  `NetworkQueue`. Pinning first checks the DHT for at least one provider, then pins —
  which fetches the content and makes this node *another provider*. So the file
  organically replicates across the hive as nodes see the announcement. (A code
  comment notes that every-node-pins-everything won't scale and proper file
  management is TODO.)
- **`SubmitRenderOffer`** → the node pins the render offer document and records the offer.
- **`CancelRenderRequest`** → handler is a TODO stub.

### 3.5 Long-term persistence: w3up / Filecoin

Pinning by online peers isn't enough if the requester goes offline before any render
node fetched the data. For that, the backend integrates **w3up (web3.storage)** as a
Filecoin-backed pinning service (`ipfs/w3up.go`):

- The backend shells out to the **`w3` CLI** (it wraps the binary, parses its stdout
  with regexes) — there is no native Go client.
- On init it runs `w3 whoami` to get the agent's DID key and `w3 space ls` to list
  available **spaces** (UCAN-authorized storage namespaces).
- Implemented operations: `Authorize(email)`, `Upload(paths)`, `Remove(cid)`,
  `UploadList`, space create/add/register/use/list, and UCAN
  delegation/proof management (`DelegationCreate`, `ProofAdd`, …).
- Files uploaded to a w3up space stay retrievable via the public gateway
  `https://<cid>.ipfs.w3s.link` even when the original node is offline.

### 3.6 Blender binaries are themselves distributed via IPFS

Render nodes don't bring their own Blender — the network pins official, hash-verified
Blender builds to a dedicated w3up space (DID in `globals/blender.go`).
`RENDERHIVE_BLENDER_ARCHIVE_FILES` maps version → `{CID, SHA-256, commit, filename}`
per OS (currently only Blender 4.0.2 / Linux).

When a render node adds a Blender version to its offer
(`RenderOffer.AddBlenderVersion`), the backend checks if the binary exists locally;
if not, it **downloads the archive by CID from the w3s gateway** with progress
reporting (`DownloadFromGateway`). Extraction of the archive and SHA verification are
still TODO. This guarantees all render nodes use bit-identical Blender builds —
important for deterministic, verifiable render output.

---

## 4. How Render Distribution Works

### 4.1 The job market: offers and requests

- **Render nodes** publish a **render offer**: which Blender versions, render engines
  (EEVEE/CYCLES), devices (CPU/CUDA/OPTIX/HIP/ONEAPI/METAL, optionally `+CPU` hybrid),
  thread counts they support, and their **price threshold** (USD cents per BBP —
  "Blender Benchmark Points"). Offers can be `Submit`ted (announced on the job queue
  topic) and `Pause`d.
- **Client nodes** publish a **render request**: the `.blend` file (by CID), required
  Blender version, render settings, and a **maximum price** per BBP, optionally
  flagging that the requesting node itself participates in rendering (`ThisNode`).

Both documents live on IPFS; only their CIDs travel over HCS. Price matching is
implicitly `request.Price >= offer.Price`.

### 4.2 Benchmark-based capacity measurement

Render power is normalized via the **official Blender benchmark tool**
(`BlenderBenchmarkTool` in `node/render.go`). The node runs
`benchmark-launcher-cli` for a given Blender version/device/scene, parses the JSON
result, and stores it under `renderhive/data/blender/blender_benchmarks/`. The key
metric is **samples per minute**, which is the basis of the BBP pricing unit
(the Blender OpenData score is the sum of samples/min across benchmark scenes).

### 4.3 Distribution timeline (hive cycles)

Job distribution is organized around hive cycles (see §2.4). The intended sequence
per cycle — visible in the phase stubs in `hivecycles.go` — is:

1. **Application phase** — available render nodes apply for work (busy nodes skip
   the cycle). *Partially implemented.*
2. **Distribution phase** — jobs from the `NetworkQueue` are matched to applying
   nodes. *TODO.*
3. **Render contract phase** — the assignment is committed via the Renderhive
   **smart contract** (`0.0.2659510` on testnet; `ContractService` methods exist for
   operator registration, staking, `AddRenderJob`, `ClaimRenderJob`, deposits/withdrawals).
   *TODO.*
4. **Validation phase** — render results are verified (dedicated HCS validation
   topic reserved). *TODO.*
5. **Claiming phase** — render nodes claim payment. *TODO.*

### 4.4 Current state of the queue

What works today: every node maintains a consensus-identical `NetworkQueue` (all
render jobs announced on the job queue topic, ordered by consensus timestamp, with
documents pinned locally) and a `NodeQueue` (jobs this node will render). Per-job HCS
topics (`JobTopics` in the node manager) are scaffolded for job-specific status
chatter. The actual matching algorithm, job execution trigger, and result submission
are not yet wired up — `BlenderAppData.Execute()` can already run Blender headless
(`-b`) and live-parse frame/memory/render-time status from its stdout, but nothing
automatically feeds queue jobs into it yet.

---

## 5. Uploading and Downloading Render Data

### 5.1 Upload paths (getting data *into* the network)

There are three implemented upload mechanisms:

1. **Local IPFS add (primary path).**
   `ipfs.Manager.AddObjectFromPath / AddObject / AddObjectFromBytes / AddDirectoryFromFiles`
   add files, in-memory byte buffers, or whole map-directories to the embedded IPFS
   node, optionally pinned. This is what `Deploy()` uses for render requests/offers.
   The node then serves that content to the swarm itself.
2. **Hash-only pre-computation.** `GetHashFromPath / GetHashFromObject` compute a CID
   *without* publishing (UnixFS `HashOnly` option) — used to reference documents by
   CID (e.g. while building a request, or to key loaded offer/request documents)
   before deciding to publish.
3. **w3up upload (persistence path).** `W3Agent.Upload(paths)` runs `w3 up` to push
   files into the active web3.storage space so they remain available via Filecoin
   after the node disconnects.

From the frontend's perspective, "uploading" a render job = HTTP JSON-RPC call to the
local backend with base64 file data → in-memory IPFS objects → pinned local add →
CID announced over Hedera. The file bytes never pass through a central server.

### 5.2 Download paths (getting data *out of* the network)

1. **IPFS get (primary path).** `ipfs.Manager.GetObject(cid, outputPath)` resolves
   the CID through the DHT, fetches the file/directory block-by-block from whatever
   peers provide it, and writes it to the local filesystem. The CLI exposes this as
   `renderhive ipfs get <cid>`.
2. **Pin (replicate without exporting).** `PinObject(cid)` verifies at least one DHT
   provider exists, then pins — used by the job-queue callback to mirror announced
   request documents and `.blend` files.
3. **HTTPS gateway download (fallback / bootstrap path).**
   `DownloadFromGateway(cid, outputPath, progressChan)` fetches
   `https://<cid>.ipfs.w3s.link` over plain HTTPS with a progress-reporting reader
   (percentage streamed through a channel). Used today for Blender binary downloads,
   and useful whenever the data is guaranteed to be in a w3up space and an HTTP
   fetch is simpler/faster than warming up the DHT.

### 5.3 Render *results*

The upload/download machinery above is generic, and the design intent (per the
`ipfs/root.go` package comment: IPFS is "used for exchange of Blender files, render
results, and other types of data") is that render nodes will publish finished frames
the same way — add result files to IPFS, pin, announce CIDs (likely on the per-job
topic), and let the requester fetch and validate them. However, **the result
submission/retrieval flow is not yet implemented** — it depends on the unfinished
distribution, validation, and claiming phases described in §4.3.

---

## 6. Quick Reference: Where to Look in the Code

| Concern | File |
|---|---|
| Embedded IPFS node, add/get/pin, swarm, gateway download | `backend/src/ipfs/root.go` |
| w3up / web3.storage / Filecoin pinning, UCAN delegations | `backend/src/ipfs/w3up.go` |
| Render requests/offers, job queue callback, Blender control, benchmarks | `backend/src/node/render.go` |
| HCS command protocol (JSON-RPC over Hedera) | `backend/src/node/commands.go` |
| Hive cycle clock & phases | `backend/src/node/hivecycles.go` |
| Node identity & manager state | `backend/src/node/root.go` |
| HCS topic create/subscribe/submit | `backend/src/hedera/consensus_service.go` |
| Topic IDs, smart contract ID, app paths, engine/device enums | `backend/src/globals/constants.go` |
| Blender binary archive (CIDs on IPFS) | `backend/src/globals/blender.go` |
| JSON-RPC API for the frontend (create/submit/cancel requests, offers) | `backend/src/jsonrpc/node.go` |
| Topic subscriptions on operator sign-in | `backend/src/jsonrpc/operators.go` |
| Manager init order, hive cycle sync loop | `backend/src/app.go` |
