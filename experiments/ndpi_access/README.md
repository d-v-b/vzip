# NDPI access: requests vs. overfetch

A benchmark of how vzip readers fetch the chunks of a virtualized Hamamatsu
NDPI slide ([profiles/ndpi.md](../../profiles/ndpi.md) §4), and of the
strategies that could make it cheaper. It starts from this observation: "the
layout is so pathological that you either have tons of requests and read the
right data or read-ahead/merging results in lots of overfetch".

The JPEG header is assumed free. It is: since VIRTUALIZE.md revision 14
(branch `fix/ndpi-header-data-source`, merged here), it lives in `data`
sources.

## The layout, in numbers

Run `prepare.py` to get these numbers. Each level of a slide has the same
number of restart intervals per MCU row, `q`: 25 for CMU-1 and 49 for
Hamamatsu-1. An interval is therefore `W / q` pixels wide: 2048 px at CMU-1
level 0 and 3840 px at Hamamatsu-1 level 0. A §4 chunk is 1 to 8 intervals
across and 128 MCU rows down.

| slide, level | size | interval | §4 chunk | one MCU row, full width |
|---|---|---|---|---|
| CMU-1 0 | 51200×38144 | 2048 px, 1.5 kB | 1×128 (2048×1024) | 38 kB |
| CMU-1 1 | 12800×9536 | 512 px, 0.4 kB | 2×128 (1024×1024) | 11 kB |
| Hamamatsu-1 0 | 188160×101376 | 3840 px, 7.7 kB | 1×128 (3840×1024) | 379 kB |
| Hamamatsu-1 2 | 47040×25344 | 960 px, 2.9 kB | 1×128 (960×1024) | 142 kB |
| Hamamatsu-1 5 | 5880×3168 | 120 px, 0.4 kB | 8×128 (960×1024) | 19 kB |

In the file, a chunk is 128 pieces, one per MCU row, and consecutive pieces
are one full row apart. Today's 64 KiB per-reference merge gap therefore
gives one of two bad outcomes:

- **Rows under 64 KiB** (all of CMU-1, Hamamatsu-1 levels 4–5): the reader
  fetches the full strip width for every chunk. That is `q / a` times the
  data, 25× at CMU-1 level 0.
- **Rows over 64 KiB** (Hamamatsu-1 levels 0–3): the reader makes 128
  requests per chunk.

`baseline.py` reproduces the header fix's measurements exactly, both in the
model and with the real Python reader through the caching proxy. The results
are in `results/baseline.md`:

| case | requests | read | needed |
|---|---:|---:|---:|
| CMU-1 level 1, all 130 chunks | 130 | 166.1 MB | 13.5 MB of chunk values |
| CMU-1 level 0, central 4×4 chunks (rows 17–20, columns 10–13) | 16 | 89.2 MB | 1.4 MB |

The prototype reader, with a batch window and a 2-byte gap, makes these 1
request for 12.9 MB and 512 requests for 1.4 MB.

**The floor.** A 1080-px-tall view covers 135 to 270 MCU rows. A reader that
fetches only the bytes it needs makes at least one request per row of the
view. Chunk shape cannot change that, because it only decides which rows are
fetched together. Only three things make those requests cheap: multi-range
requests, HTTP/2 multiplexing, or reading the bytes between rows.

## Files

| file | what it does |
|---|---|
| `prepare.py` | Virtualizes CMU-1.ndpi and Hamamatsu-1.ndpi with the browser virtualizer through the caching proxy (`conformance/virtualize/proxy.py`, started on port 8791 if absent, cache `/tmp/vzip-proxy-cache`). Reads every chunk reference back and writes each level's interval table to `cache/<slide>.npz`. Reads structure only. |
| `model.py` | Layouts; chunkings (§4 and alternatives, with the §1.2 payload cap); viewer traces; read planners; the network model. |
| `bench.py` | Runs traces through `<chunking>:<planner>` specs and prints per-network tables (`--per-view` for each view). |
| `run_experiments.py` | The experiments below, written to `results/experiments.md` and `.json`. |
| `baseline.py` | The two baseline cases above (`--real` also runs the Python reader). |
| `probe_hosts.py` | For each corpus host: HTTP/2 (ALPN), multi-range, RTT and bandwidth. |
| `validate.py` | Checks the model against a shaped local server (`local`) and against openslide.cs.cmu.edu (`remote`). |
| `reader_bench.py` | Times the prototype Python reader flags on the shaped server, next to the model. |

```
uv run python experiments/ndpi_access/prepare.py          # once, ~10 s
uv run python experiments/ndpi_access/run_experiments.py  # ~5 s
uv run python experiments/ndpi_access/bench.py --nets h1-20ms-50M --planners spec:pc64k,spec:bopt+c
uv run python experiments/ndpi_access/baseline.py --real
uv run python experiments/ndpi_access/validate.py local
uv run python experiments/ndpi_access/reader_bench.py 4    # ~10 min, real time
```

### Traces

The viewer is Neuroglancer-like, at 1920×1080. Its zoom `z` is level-0
pixels per screen pixel, and it shows the coarsest level with at least one
data pixel per screen pixel. Each view requests every chunk it covers, all
at once. A chunk cache per trace means chunks seen before are not requested
again. There are four traces per slide, 56 views in all:

- `overview`: the whole slide fitted to the screen.
- `zoom`: from the overview, `z` halves at each step down to `z = 1`. The
  views are centred on the densest tissue: the 16-row window with the most
  compressed bytes at a middle level.
- `pan_L0`: 12 half-screen steps across, then 12 down, at level 0.
- `pan_mid`: the same at a middle level (level 1 for CMU-1, level 2 for
  Hamamatsu-1).

### Metrics

| metric | what it counts |
|---|---|
| requests | HTTP requests |
| fetched | response body bytes |
| overfetch | fetched ÷ the bytes of the intervals under the visible region, counted once per trace. Any reader must fetch those bytes. |
| px | newly decoded pixels ÷ visible pixels: the viewer's decode overfetch, which matters when comparing chunk shapes |
| total s | the sum over views of the modelled time-to-complete |

### Network model

`model.simulate` takes all of a view's requests at t = 0 and runs them over
`conns` slots: 6 for HTTP/1.1 and 100 for HTTP/2-like. Each request holds its
slot for one RTT, then for its transfer. All transfers share the bandwidth
equally (processor sharing). Each response adds 400 B of headers, and each
part of a multipart response adds 90 B. The model ignores TCP slow start,
server time beyond the RTT, and browser overhead.

| network | RTT | bandwidth | connections |
|---|---|---|---|
| `h1-20ms-50M`, `h1-80ms-50M`, `h1-20ms-500M`, `h1-80ms-500M` | 20 or 80 ms | 50 or 500 Mbit/s | 6 |
| `h2-20ms-50M`, `h2-80ms-500M` | 20 or 80 ms | 50 or 500 Mbit/s | 100 |
| `openslide`: openslide.cs.cmu.edu as measured | 500 ms | 5.3 Mbit/s | 6 |

### Validation

Run `validate.py local`: on a server that sleeps the RTT and paces all
responses through one rate limit, the model is within 2–8% of the measured
time. The cases were from 1 to 256 requests over 6 or 100 connections, on
three of the networks.

Run `validate.py remote` against openslide.cs.cmu.edu. It measured a 524 ms
time to first byte and 7.1 Mbit/s on one connection, 5.6 Mbit/s on six.

| case | requests | MB | model (shared bandwidth) | measured |
|---|---:|---:|---:|---:|
| `pan_L0[1]`, batched exact | 256 | 2.30 | 23.3 s | 23.1 s |
| `pan_mid[1]`, per-chunk 64 KiB | 6 | 6.77 | 10.2 s | 9.0 s |
| `pan_mid[1]`, batched exact | 256 | 1.07 | 22.9 s | 22.5 s |
| overview, per-chunk 64 KiB | 12 | 3.31 | 5.7 s | 6.5 s |
| overview, batched exact | 1 | 0.83 | 1.7 s | 3.8 s |

The single-request case is the model's blind spot. With a 0.5 s RTT, TCP
slow start needs about 6 RTTs to open the window, so the model flatters
large single requests on high-latency links.

Run `reader_bench.py`: the prototype Python reader, on the shaped server with
6 threads, comes within 5–25% of the model. The reader is the slower side,
from per-request Python overhead when there are many small requests.

| reader (CMU-1, 4 views each) | network | requests | MB | measured | model |
|---|---|---:|---:|---:|---:|
| today, `pan_L0` | 20 ms, 50 Mbit/s | 6 | 25.5 | 4.29 s | 4.15 s |
| batched, cost model and cache, `pan_L0` | 20 ms, 50 Mbit/s | 756 | 2.6 | 3.07 s | 2.67 s |
| batched, cost model and cache, `pan_mid` | 20 ms, 50 Mbit/s | 24 | 2.3 | 0.62 s | 0.44 s |
| batched exact, `pan_mid` | 80 ms, 500 Mbit/s | 1024 | 1.8 | 14.6 s | 13.8 s |
| batched, cost model and cache, `pan_L0` | 80 ms, 500 Mbit/s | 18 | 8.4 | 0.45 s | 0.37 s |
| Hamamatsu-1 today, `pan_mid` | 80 ms, 500 Mbit/s | 1536 | 6.2 | 21.8 s | 20.6 s |
| Hamamatsu-1 batched, cost model and cache, `pan_mid` | 80 ms, 500 Mbit/s | 24 | 41.0 | 1.27 s | 0.98 s |
| Hamamatsu-1 batched, cost model and cache, `pan_L0` | 80 ms, 500 Mbit/s | 12 | 101.7 | 2.39 s | 1.79 s |

All of it is in `results/reader_bench.md`. The prototype realizes the
modelled gains. For Hamamatsu-1 at 80 ms, the measured speedup is 7× on
`pan_mid` (8× in the model). The large merged reads cost the Python reader
more than the model says (+30%).

### Corpus hosts

From `probe_hosts.py`, on 2026-10-04:

| host | HTTP/2 | multi-range | TTFB | Mbit/s, 1 connection |
|---|---|---|---:|---:|
| openslide.cs.cmu.edu (the NDPI slides) | no | no: answers with the first range only | 524 ms | 5–7 |
| zenodo.org | no | no: one range | 80 ms | 12 |
| ftp.ebi.ac.uk (HTTPS) | no | **yes**, 206 multipart/byteranges | 30 ms | 20 |
| S3, janelia-cosem-datasets | no | no: 200 with the whole object | 142 ms | — |
| storage.googleapis.com | yes | no: 400 | 184 ms | 53 |
| uk1s3.embassy.ebi.ac.uk (Ceph S3) | no | no: one range | 55 ms | — |

Browsers add two costs to multi-range requests. A `Range` header with more
than one range is not CORS-safelisted, so it needs a preflight (cacheable
with `Access-Control-Max-Age`). And the multipart body has to be parsed.

## Results

Each cell is `total s · requests · overfetch` over all 56 views. The full
tables, for all 7 networks, are in `results/experiments.md`. The reader-side
rows use §4 chunks unless a row says otherwise.

**CMU-1**

| strategy | 20 ms, 50 Mbit/s (HTTP/1.1) | 80 ms, 500 Mbit/s (HTTP/1.1) | 20 ms, 50 Mbit/s (HTTP/2) | 80 ms, 500 Mbit/s (HTTP/2) | openslide |
|---|---:|---:|---:|---:|---:|
| today: per chunk, gap 64 KiB | 41.4 s · 104 · 13.9× | 6.9 s · 104 · 13.9× | 41.4 s · 104 · 13.9× | 6.7 s · 104 · 13.9× | 401 s · 104 · 13.9× |
| per chunk, exact (gap 2 B) | 44.9 s · 12968 · 1.4× | 173.8 s · 12968 · 1.4× | 6.5 s · 12968 · 1.4× | 11.8 s · 12968 · 1.4× | 1098 s · 12968 · 1.4× |
| per chunk, gap = bw·rtt/conns | 28.6 s · 4616 · 5.2× | 6.9 s · 104 · 13.9× | 6.5 s · 12592 · 1.4× | 6.6 s · 187 · 13.7× | 401 s · 104 · 13.9× |
| batched, exact | 25.2 s · 6913 · 1.4× | 93.6 s · 6913 · 1.4× | 5.7 s · 6913 · 1.4× | 7.4 s · 6913 · 1.4× | 598 s · 6913 · 1.4× |
| batched, cost-model gap | 17.3 s · 2983 · 3.2× | 5.0 s · 193 · 8.0× | 5.7 s · 6885 · 1.4× | 4.0 s · 3200 · 4.5× | 239 s · 193 · 8.0× |
| **batched, cost-model gap, span cache** | 14.2 s · 2983 · 2.1× | 3.9 s · 193 · 4.2× | 5.7 s · 6885 · 1.4× | 3.4 s · 3200 · 2.5× | 134 s · 193 · 4.2× |
| 64 KiB block cache, adjacent blocks joined | 13.2 s · 18 · 4.4× | 2.7 s · 18 · 4.4× | 13.2 s · 18 · 4.4× | 2.7 s · 18 · 4.4× | 130 s · 18 · 4.4× |
| whole-row read-ahead | 13.1 s · 18 · 4.4× | 2.7 s · 18 · 4.4× | 13.1 s · 18 · 4.4× | 2.7 s · 18 · 4.4× | 129 s · 18 · 4.4× |
| multi-range, batched, span cache (where the host allows it) | 4.8 s · 193 · 1.4× | 3.1 s · 193 · 1.4× | 5.0 s · 3201 · 1.4× | 3.1 s · 3201 · 1.4× | not available |
| 4096×256 chunks (spec change), today's reader | 16.3 s · 136 · 5.3× | 4.6 s · 136 · 5.3× | 16.3 s · 136 · 5.3× | 4.2 s · 136 · 5.3× | 164 s · 136 · 5.3× |
| 4096×256 chunks, batched, cost-model gap, span cache | 9.5 s · 1700 · 1.6× | 3.7 s · 188 · 3.4× | 4.5 s · 3298 · 1.2× | 3.1 s · 2373 · 1.5× | 112 s · 188 · 3.4× |
| bound: one exact request per view (§4 chunks) | 4.8 s · 33 · 1.4× | 3.1 s · 33 · 1.4× | 4.8 s · 33 · 1.4× | 3.1 s · 33 · 1.4× | 55 s · 33 · 1.4× |

**Hamamatsu-1**

| strategy | 20 ms, 50 Mbit/s (HTTP/1.1) | 80 ms, 500 Mbit/s (HTTP/1.1) | 20 ms, 50 Mbit/s (HTTP/2) | 80 ms, 500 Mbit/s (HTTP/2) | openslide |
|---|---:|---:|---:|---:|---:|
| today: per chunk, gap 64 KiB | 68.2 s · 11956 · 3.9× | 162.8 s · 11956 · 3.9× | 35.9 s · 11956 · 3.9× | 14.1 s · 11956 · 3.9× | 1258 s · 11956 · 3.9× |
| per chunk, exact (gap 2 B) | 64.4 s · 17748 · 1.4× | 237.9 s · 17748 · 1.4× | 15.1 s · 17748 · 1.4× | 16.3 s · 17748 · 1.4× | 1524 s · 17748 · 1.4× |
| per chunk, gap = bw·rtt/conns | 61.9 s · 13973 · 2.6× | 44.3 s · 145 · 47× | 15.1 s · 17748 · 1.4× | 14.1 s · 11956 · 3.9× | 1258 s · 11956 · 3.9× |
| batched, exact | 34.2 s · 7937 · 1.4× | 107.6 s · 7937 · 1.4× | 14.1 s · 7937 · 1.4× | 9.0 s · 7937 · 1.4× | 724 s · 7937 · 1.4× |
| batched, cost-model gap | 34.0 s · 7559 · 1.5× | 29.7 s · 211 · 31× | 14.1 s · 7937 · 1.4× | 8.6 s · 7201 · 2.1× | 693 s · 7169 · 1.7× |
| **batched, cost-model gap, span cache** | 34.0 s · 7559 · 1.5× | 17.0 s · 211 · 16× | 14.1 s · 7937 · 1.4× | 8.6 s · 7201 · 2.1× | 693 s · 7169 · 1.7× |
| 64 KiB block cache, adjacent blocks joined | 53.0 s · 3283 · 5.7× | 47.9 s · 3283 · 5.7× | 50.8 s · 3283 · 5.7× | 9.2 s · 3283 · 5.7× | 601 s · 3283 · 5.7× |
| whole-row read-ahead | 145.9 s · 21 · 17× | 16.2 s · 21 · 17× | 145.9 s · 21 · 17× | 16.2 s · 21 · 17× | 1383 s · 21 · 17× |
| multi-range, batched, span cache (where the host allows it) | 13.3 s · 211 · 1.4× | 4.1 s · 211 · 1.4× | 13.5 s · 3501 · 1.4× | 4.2 s · 3501 · 1.4× | not available |
| 4096×256 chunks (spec change), today's reader | 32.6 s · 5208 · 2.2× | 71.7 s · 5208 · 2.2× | 19.9 s · 5208 · 2.2× | 7.4 s · 5208 · 2.2× | 564 s · 5208 · 2.2× |
| 4096×256 chunks, batched, cost-model gap, span cache | 25.3 s · 3784 · 1.7× | 14.6 s · 211 · 13× | 15.4 s · 4034 · 1.6× | 5.6 s · 3798 · 1.8× | 427 s · 3778 · 1.7× |
| bound: one exact request per view (§4 chunks) | 13.2 s · 36 · 1.4× | 4.1 s · 36 · 1.4× | 13.2 s · 36 · 1.4× | 4.1 s · 36 · 1.4× | 136 s · 36 · 1.4× |

The bound `interval:ideal` is the visible intervals only, in one request per
view. It comes to 3.9 to 3.8 s for CMU-1 and 9.7 to 4.6 s for Hamamatsu-1. In
the high-latency cases it is slower than the §4 bound, because the views
whose chunks are already cached cost a request there too.

## Iterations

Each experiment changes one thing from the one before.

| # | change | result |
|---|---|---|
| E0 | Baseline: today's per-reference 64 KiB gap | 14× overfetch for CMU-1 (it reads the full width); 12k requests for Hamamatsu-1. 4–40× from the bound. |
| E1 | Per-reference gap: 2 B, 64 KiB, 256 KiB, 1 MiB, bw·rtt, bw·rtt/conns, cost model | No per-reference gap works on HTTP/1.1. Every gap lands on one side of a cliff at one row's bytes. Exact reads pay off only on HTTP/2 (CMU-1 41 → 6.5 s). |
| E2 | Cross-chunk batching: all of a view's reads in one plan | The rows of horizontally adjacent chunks join, so requests drop by about the number of chunk columns (Hamamatsu-1: 17.7k → 7.9k, 64 → 34 s). Adding the cost-model gap crosses rows only where that pays (Hamamatsu-1 at 80 ms, 500 Mbit/s: 163 → 30 s). |
| E3 | Caches | A span cache keeps the fetched gaps, which are the same MCU rows' other intervals, so the next pan finds them already read. CMU-1 17.3 → 14.2 s; Hamamatsu-1 at 80 ms, 500 Mbit/s 29.7 → 17.0 s. Whole-row read-ahead and 64 KiB blocks win where rows are small (CMU-1) and lose 4× where they are large (Hamamatsu-1 at 50 Mbit/s). The cost model chooses between the two by itself. |
| E3b | Cost model discount 0.5 and 0.25 (prices in later reuse of the gap bytes) | Mixed: ±10%, and worse in places. Not kept. |
| E4 | Multi-range requests, batched, up to 200 ranges per request, at least one request per connection | At the bound everywhere. Hamamatsu-1: 13.3 s against a 13.2 s bound; 4.1 s at 80 ms. Of the corpus hosts, only ftp.ebi.ac.uk answers them. |
| E5 | Chunk shape (spec change) with today's reader | 4096×256 halves the time (CMU-1 41 → 16 s, Hamamatsu-1 68 → 33 s) at a similar decode cost (px 0.97 and 1.36). 8192×128 is a little better on HTTP/1.1 but decodes 1.5–2.2×. Full-width bands decode 4.5–15× and are slow at level 0. Bands for levels at most 2 screens wide change almost nothing, because coarse levels are a small share of the traffic. |
| E6 | Chunk shape with the batched cost-model reader | 4096×256 still helps (CMU-1 14.2 → 9.5 s, Hamamatsu-1 34 → 25 s), but much less than the reader change does. |
| E7 | Chunk shape with multi-range | 4096×256: CMU-1 4.8 → 4.3 s; Hamamatsu-1 13.3 → 15.1 s (wider chunks overfetch). No gain. |

## What works, what doesn't, and why

- **Batching across chunks is the main win, and it is reader-only.** The
  rows of the chunks in one view are contiguous across chunk boundaries.
  Planned together, they cost one request per MCU row of the view instead of
  one per row per chunk column. Planned alone, a reader cannot know which
  neighbours will follow.
- **The cost model resolves the dilemma from the opening quote.** "Tons of
  requests" or "overfetch" is a choice to make per view, from
  `ceil(N / conns) · rtt` against `gap bytes / bandwidth`. No fixed gap gets
  it right, because one row's bytes range from 11 kB to 379 kB across the
  corpus. For a reader that sees one chunk at a time, `bw·rtt/conns` is the
  best fixed rule, and it is still bad.
- **A span cache turns overfetch into prefetch.** The bytes between a view's
  row pieces are the neighbouring chunks' pieces of the same rows. A pan
  across the slide uses them next.
- **Multi-range requests and HTTP/2 make exact reads cheap.** They are the
  only ways to reach the bound without overfetch. But the hosts of the NDPI
  corpus support neither. openslide.cs.cmu.edu spends 0.5 s per request at
  5 Mbit/s, so even the bound is 55 s (CMU-1) and 136 s (Hamamatsu-1) over
  these traces. That host is slow for any layout.
- **Block caches and whole-row read-ahead** are fixed choices of the same
  trade-off. Each is good on one slide and bad on the other.
- **Chunk shape is second order.** It cannot lower the floor of one request
  per MCU row. A wider, shorter chunk has fewer rows per chunk and is closer
  to the viewport's aspect ratio. That helps today's reader about 2× and the
  batched reader 1.3–1.5×.

## Recommendation

### Reader side (no spec change)

Prototypes behind flags, with defaults unchanged:

- `VZipStore(merge_gap=..., batch_window=..., span_cache=..., net=...)` in
  `src/vzip/store.py`, with the planning in `src/vzip/readplan.py`.
- `Archive.open(..., { mergeGap, batchWindowMs, spanCacheBytes })` in
  `web/src/archive.ts`, passed through `makeHandler({ readOptions })`.

1. **Batch window.** Plan the url reads of all values requested within a few
   milliseconds of each other together.
   - In the browser, put it in `Archive.read`. The service worker calls it
     once per chunk fetch event, and a viewer's chunk requests for one view
     arrive together.
   - In Python, put it in `VZipStore._ref_bytes`, using an asyncio timer.
     zarr's `async.concurrency` (default 10) caps how many chunk reads are
     in flight, and so how large a batch can be. Raise it for slides.
2. **Cost-model merging** in place of the fixed 64 KiB gap. Merge the
   smallest gaps first, while `ceil(N / conns) · rtt + bytes / bw` drops.
   Estimate RTT and bandwidth from the reader's own requests (`NetEstimate`).
   Take `conns` as 6 for HTTP/1.1 hosts. The browser's HTTP version is
   visible as `PerformanceResourceTiming.nextHopProtocol`.
3. **A span cache** of fetched runs (LRU, tens to hundreds of MB), with
   in-flight deduplication.
4. **Multi-range requests** only for hosts that pass a probe (a two-range
   request answered with 206 multipart). Allow up to about 100–200 ranges
   per request and at least one request per connection, and expect a CORS
   preflight. This is worth doing for EBI-hosted inputs. It needs a
   multipart parser in both readers; the prototype does not include one.

Expected gains, before multi-range, over the 56 views: CMU-1 41 → 14 s at
20 ms and 50 Mbit/s, and 6.9 → 3.9 s at 80 ms and 500 Mbit/s; Hamamatsu-1
68 → 34 s and 163 → 17 s. Over the baseline cases: 130 requests and
166 MB → 1 request and 12.9 MB, and 16 requests and 89 MB → about 510
requests and 1.4 MB. The second case is faster from 80 ms up, or 6 requests
with multi-range.

### Spec changes worth proposing

- **Wider, shorter NDPI chunks**, about 4096 × 256 px: `a = max(1,
  floor(4096 / (R·mw)))`, `b = 256 / mh`, within the same payload cap.
  - Gain: 2× for readers that read one chunk at a time, 1.3–1.5× for batched
    readers, and no gain with multi-range.
  - Cost: about the same decoded pixels per view at 1920×1080 (px 0.97
    against 1.05 for §4 on CMU-1, and 1.36 against 1.25 on Hamamatsu-1, where
    an interval is 3840 px wide at level 0 and the chunk cannot be
    narrower). There are 4× as many chunk
    rows, but the total chunk count is unchanged at the same area. A
    portrait viewport, or tall views, would decode more. It is a breaking
    change of chunk keys and shapes for existing archives.
  - Verdict: worth it only if readers without batching (other zarr clients)
    matter. Prefer the reader changes.
- **Not recommended:** full-width bands, at any level. Bands only for coarse
  levels gain nothing measurable. 8192 × 128 decodes 1.5–2.2× and is no
  faster.

## Open questions for the collaborator

1. Do Neuroglancer's chunk requests for a view really reach the service
   worker within a few milliseconds of each other? Its chunk queue,
   priority tiers and download concurrency limits decide how much a batch
   window can see.
2. What span cache budget is acceptable in a service worker? Neither
   prototype has a memory policy beyond LRU by bytes.
3. Do other NDPI files have other interval counts per row? Both slides here
   have the same `q` at every level, 25 and 49. The whole trade-off scales
   with one row's bytes, `q × interval bytes`. Hamamatsu-2 is in the corpus
   but untested.
4. Is there a host for NDPI slides with HTTP/2 or multi-range support
   (CloudFront, nginx)? The model says exact reads would then be within 10%
   of the bound. Measure it for real.
5. TCP slow start is outside the model. It flatters large merged reads on
   high-RTT links (see the openslide validation). The cost model could add a
   slow-start term, or prefer a few medium requests over one large one.
6. Would a server-side or one-off re-encoding of the strip into tiles be
   acceptable for the slides that matter most? It is outside vzip's
   no-copy model, but it is the only change that removes the per-row floor
   without multi-range or HTTP/2.
