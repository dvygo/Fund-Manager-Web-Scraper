# Pipeline Phases

## Phase 1 — AMC seed list (implemented: `main.py`)

Scrape the AMFI members directory, normalize firm names, resolve corporate domains, write `data/amc_seed_list.json`.

- Live scrape via Crawl4AI (`AsyncWebCrawler`, headless Chromium, cache bypass, defensive render wait).
- Name cleaning strips legal suffixes ("Mutual Fund", "Asset Management Company", "Ltd", …).
- Domain resolution: `KNOWN_DOMAINS` curated map → substring match → `www.{slug}mf.com` guess.
- Resilience: if the live parse returns fewer than 20 names, an embedded static roster of active AMCs is used instead, and the output records the source. The script always produces a seed file.

## Phase 2 — Team page discovery (planned)

For each seed record, crawl `base_domain`, validate the domain resolves, and locate the actual fund-managers/team page (checking `team_url_guess`, sitemaps, nav links). AMFI's per-member detail pages (`https://www.amfiindia.com/member/{id}`) can cross-check corporate websites. Output: verified `team_url` per AMC.

## Phase 3 — Fund manager extraction (planned)

Crawl each verified team page and extract structured manager profiles: name, designation, funds managed, experience, qualifications. LLM-assisted extraction (vLLM serving Qwen, OpenAI-compatible endpoint on :8000 — see `docker/docker-compose.yml`) since page structures vary per AMC.

## Phase 4 — Persistence (planned)

Land raw HTML snapshots and extracted JSON in a MinIO data lake (service defined in `docker/docker-compose.yml`, S3 API on :9000), for downstream querying and change tracking. Immutable, date-partitioned — every pull kept, nothing overwritten:

```
bucket mf-raw-html/   {crawl_date}/{base_domain}/{page}.html   (each page = one object)
bucket mf-extracted/  {crawl_date}/{base_domain}.json          (Phase 3 output)
```

## Phase 5 — Semantic search (planned)

Qdrant (:6333) holds the *latest* state: one point per manager (keyed by manager+firm, upserted each pull) with the extracted profile as payload. Role split:

- **Embedding model** (separate small model, e.g. sentence-transformers/BGE on CPU — not the chat Qwen) vectorizes profiles and queries.
- **Qdrant** does the actual search: cosine top-k over vectors. No LLM in the lookup.
- **Qwen via vLLM** optionally does RAG on top: takes Qdrant hits, writes a readable answer (Open WebUI on :3000 as the interface).

MinIO = dated immutable archive; Qdrant = current snapshot, searchable.
