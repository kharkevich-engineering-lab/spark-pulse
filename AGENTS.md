# Spark Pulse — Agent Pointer

Spark Pulse is a FastAPI + React control plane and CLI for deploying and monitoring inference engines (vLLM, SGLang) on NVIDIA DGX Spark clusters. A control-plane process coordinates; a Rust agent runs on each node and does the actual work — starting containers, reading GPU stats, configuring the network fabric — so "deploy" and "monitor" always mean "ask a node's agent," never "do it locally," including on the machine the control plane itself runs on.

This file used to carry a full architecture writeup. It drifted out of date with nothing to catch it, which is worse than no writeup at all, so it has been cut down to a pointer:

- **[`CLAUDE.md`](CLAUDE.md)** — the authoritative, maintained guide to this repo's architecture, commands, and conventions. Read it first, whether you are a person or an agent.
- **[`docs/`](docs/)** — the user-facing documentation site (published at the GitHub Pages URL in `README.md`), covering the product tour, the agent protocol, and the configuration/CLI/API/MCP reference. It stays honest because `scripts/check-docs-links.py` fails CI on a broken or orphaned page.
- **[`README.md`](README.md)** — the project's public-facing summary and feature list.
