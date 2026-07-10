# Data Schema

## `data/amc_seed_list.json` (Phase 1 output)

JSON array, one object per AMC:

| Field | Type | Meaning |
|---|---|---|
| `amc_id` | integer | Sequential ID (1-based) within this seed list run. Not stable across runs — the roster order can change. |
| `firm_name` | string | Firm name as listed by AMFI (or the static roster), e.g. `"ICICI Prudential Mutual Fund"`. |
| `clean_name` | string | Core name after stripping legal suffixes, e.g. `"ICICI Prudential"`. Join key for domain mapping. |
| `base_domain` | string | Corporate domain — curated mapping when known, `www.{slug}mf.com` guess otherwise. No scheme. |
| `team_url_guess` | string | Unverified guess at the fund-managers page: `https://{base_domain}/fund-managers`. Phase 2 validates/replaces it. |

Example object:

```json
{
  "amc_id": 17,
  "firm_name": "ICICI Prudential Mutual Fund",
  "clean_name": "ICICI Prudential",
  "base_domain": "icicipruamc.com",
  "team_url_guess": "https://icicipruamc.com/fund-managers"
}
```

Caveats:

- `base_domain` from the slug fallback is a guess and may not exist; treat unvalidated domains as hints.
- Output source (live scrape vs static fallback) is logged at run time, not stored in the file.
