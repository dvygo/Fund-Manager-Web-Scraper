# MF-Engine

Fund-manager extraction pipeline for Indian AMCs. Phase 1 scrapes the AMFI members directory into a crawler seed list (`data/amc_seed_list.json`).

See [CLAUDE.md](CLAUDE.md) for setup/commands and [context/](context/) for pipeline docs.

## Diagrams

- [High-level flowcharts](docs/high-level-flowchart.md) — full pipeline (Phases 1–5) and data-store flow
- [High-level sequences](docs/high-level-sequence.md) — end-to-end pipeline run and RAG chat query
- [Phase 1 low-level flowcharts](docs/low-level-phase1-flowchart.md) — `main.py` control flow, domain resolution, name cleaning
- [Phase 1 low-level sequences](docs/low-level-phase1-sequence.md) — call-level trace with failure paths, container lifecycle
