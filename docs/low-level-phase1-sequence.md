# Phase 1 Scraper (`main.py`) — Low-Level Sequence

## Call-level sequence with failure paths

```mermaid
sequenceDiagram
    participant M as main()
    participant F as fetch_members_html()
    participant C as AsyncWebCrawler
    participant B as Chromium (Playwright)
    participant A as amfiindia.com
    participant P as parse_member_names()
    participant BS as BeautifulSoup
    participant R as build_records()
    participant FS as data/amc_seed_list.json

    M->>F: await
    F->>C: async with AsyncWebCrawler(BrowserConfig)
    C->>B: launch headless, Chrome UA
    F->>C: arun(url, CrawlerRunConfig)
    C->>B: navigate + wait_for css:body + 4s settle
    B->>A: GET /aboutamfi?tab=members
    A-->>B: JS-rendered members tab

    alt crawl succeeds
        B-->>C: DOM captured
        C-->>F: result (success, html)
        F-->>M: html
    else network drop / bot block / timeout
        C-->>F: result.success = false or exception
        F-->>M: None (logged)
    end

    opt html present
        M->>P: parse_member_names(html)
        P->>BS: soup.select("a[href*='/member/']")
        alt >= 20 member anchors
            BS-->>P: anchor texts
            P-->>M: deduped names (primary path)
        else layout changed / partial render
            P->>BS: find_all leaf nodes, regex filter
            BS-->>P: candidate strings
            P-->>M: deduped names (fallback scan)
        end
    end

    alt len(names) < 20
        M->>M: names = STATIC_AMC_NAMES (49, source=static_fallback)
    else live parse healthy
        M->>M: source=live
    end

    M->>R: build_records(names)
    loop each firm name
        R->>R: clean_name — strip legal suffixes
        R->>R: resolve_domain — exact, partial longest-first, slug guess
        R->>R: team_url_guess = https://{domain}/fund-managers
    end
    R-->>M: records

    M->>FS: mkdir data/, write JSON indent=2
    M-->>M: log count + source, exit 0
```

## One-shot container lifecycle

```mermaid
sequenceDiagram
    actor U as User
    participant DC as docker compose
    participant IMG as image cache
    participant CT as mf-scraper container
    participant VOL as ../data bind mount

    U->>DC: docker compose run --rm scraper
    DC->>IMG: build from Dockerfile (cached after first)
    DC->>CT: create fresh container
    CT->>CT: CMD python main.py
    CT->>VOL: write /app/data/amc_seed_list.json
    CT-->>DC: exit 0
    DC->>CT: --rm deletes container
    Note over VOL: JSON persists on host disk;<br/>container is gone
```
