# A tour of the UI

Every screenshot here comes out of simulation mode, captured by `web/scripts/capture-screenshots.mjs` against a simulated two-node cluster. Re-run it and you get these images again — a page that changes and a screenshot that does not is documentation that lies.

## Recipes & Mods

![Recipes and mods](../assets/screenshots/recipes.png)

A recipe is a model, an engine and its arguments in one file. The card says which engines can serve it, where it came from (bundled, `custom-`, or installed from an OCI collection), and whether it fits on one machine or wants several. A recipe already running is marked, so you do not deploy a second copy of the same thing by accident.

**Custom mode** switches the page to what you wrote yourself: your own recipes and mods, with an editor and a delete on each card rather than buried inside a drawer.

## Inference

![The Inference page with a deployment expanded](../assets/screenshots/jobs.png)

What is running, and what each one is doing. A row carries the engine, the port, health, lifecycle status and — while a change is being applied — a second chip saying *in progress* or *deleting*, because a deployment being torn down is still running until a node says otherwise.

Expanding a row shows the resolved engine and image, the container name, the per-rank state for a multi-node deployment, and the engine's own metrics: requests running, queue depth, KV-cache use, preemptions. That window is read from the engine's `/metrics` and held in memory only — an hour of five-second samples, gone on restart. Retention is Prometheus's job, and the page says so rather than implying otherwise.

Below that, the log, streamed.

## Monitoring

![Monitoring, one section per node](../assets/screenshots/monitoring.png)

Every registered node, asked for its own stats through its own agent — including the machine the control plane runs on. Each section carries that node's GPUs, host memory and disks.

Three things this page is careful about:

- **A GB10 reports no GPU memory.** `nvidia-smi` returns `[N/A]` because the pool is unified, so the card says *unified memory — usage not reported by nvidia-smi* instead of drawing an empty bar for a full machine.
- **A node that could not be asked keeps its section** and says why. A missing section and an idle machine look identical, and only one of them is fine.
- **A GPU process names the deployment holding it.** The node says which container the process is in; the control plane knows which containers it started. Anything else is marked *untracked*, which is the row an operator opens this page for.

The **Kill** button stops the container when the process is in one of ours — killing the process inside would leave the container holding its ports — and otherwise signals the process on the node that has it.

## Models

![Models](../assets/screenshots/models.png)

The Hugging Face cache as a catalogue: what is downloaded, how big, which recipes reference it, and what the config says about precision and context length. Downloads run as tracked jobs with progress, and a download started because a deploy needed it says which deployment is waiting.

Deleting a model asks *which machines* — a 26 GB model replicated to four Sparks is on four disks, and the dialog preselects the nodes that presence says hold a copy.

## Engines

![Engines](../assets/screenshots/engines.png)

An engine is a plugin plus a published image. This page is both: what each engine supports and which nodes carry its image. Expanding a row asks every node whether it has that image and at which ID — *unknown* for a node that could not be asked, never *absent*, because "we could not ask" is not a reason to pull 26 GB again.

Pull to a node, remove from several, and set which engine indexes are consulted.

## Cluster

![Cluster](../assets/screenshots/cluster.png)

The node registry and what is running on it. Adding a node takes an address; discovery offers what it found over mDNS, and nothing is ever required to come from discovery. Each node's diagnostics name what is wrong and what to do about it.

Multi-node is marked experimental here in one line — the full account of what is unproven belongs where you are about to act on it, which is the deploy form and the expanded row on Inference.

## OCI Registry

![OCI registry](../assets/screenshots/oci.png)

Recipe collections published as OCI artifacts. Add a registry, browse a collection, install a recipe, and let a background check tell you when a newer version is published.

## Cache

![Cache](../assets/screenshots/cache.png)

The caches that fill a Spark's disk — Hugging Face, vLLM, FlashInfer, Triton, ccache, wheels — with their sizes and a way to clear them.

## MCP

![MCP](../assets/screenshots/mcp.png)

The Model Context Protocol endpoint: which tools are exposed and how to point an assistant at them. Each tool is implemented by calling this app's own REST API, so MCP behaviour is the REST behaviour by construction.

## Settings

![Settings](../assets/screenshots/settings.png)

Tabbed over one form: deployment defaults, container profile, cluster, engines, preferences (theme and language, both browser-local), secrets, and an **Environment** tab that reports how the process is configured — database, CORS origins, auth, MCP — without a browser being able to change any of it. Passwords in a database URL come back stripped.
