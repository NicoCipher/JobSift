# Lever public postings

JobSift collects Lever boards through the unauthenticated public postings list
API, using an explicit target made of a Lever instance and site.

| Instance | Endpoint |
| --- | --- |
| `global` | `https://api.lever.co/v0/postings/{site}` |
| `eu` | `https://api.eu.lever.co/v0/postings/{site}` |

The target identity is `instance:site`; a posting identity is the tuple of
instance, site, and Lever's non-empty provider ID. This prevents an ID in an EU
site from colliding with the same ID in a global site.

## Collection contract

Requests use `mode=json`, `skip`, and `limit=50`. Collection begins at zero,
advances `skip` by 50, and stops on a short page. An empty first page is a
valid empty board. Repeated page content ends collection as partial rather than
claiming complete coverage. The collector makes no per-posting detail calls:
the completed contract audit found no additional normalization evidence there.

`hostedUrl` is the job and canonical URL, and `applyUrl` is the application
URL. The collector prefers `descriptionPlain`, then `descriptionBodyPlain`,
then HTML text. It preserves `location`, `allLocations`, team, department,
country, workplace type, commitment, and `createdAt` as source evidence.
Only safely mapped ISO country codes contribute semantic country eligibility.
Created time is retained as provenance, not claimed as a posting or update
timestamp. Workplace type is limited to the provider's observed `remote`,
`hybrid`, and `onsite` values; future values are unknown.

Malformed postings are quarantined and make an otherwise collected board
partial. Board 404 is an invalid target; exhausted 429, network, and 5xx
retries map to rate-limited, network failure, and provider error respectively.
No absence from a later list is asserted to mean a closed vacancy, and repost
continuity beyond observed provider IDs remains unproven.

## CLI

```sh
job-scout collect --source lever --lever-instance global --board acme \
  --company Acme --client config/search_briefs/taiwo_operator_sourcing_v1.json
```

The empirical contract and frozen validation evidence live in
[`validation/lever_contract_v1/`](../validation/lever_contract_v1/).
