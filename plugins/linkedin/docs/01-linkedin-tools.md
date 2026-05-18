# LinkedIn plugin — tool catalog

Active only on `*.linkedin.com`. No API key, no OAuth — reads the
user's currently-loaded LinkedIn page DOM via a browser primitive.

## Page-shape detector

| Page | URL | `page_type` |
|---|---|---|
| Feed / home | `/` or `/feed/...` | `feed` |
| Member profile | `/in/<id>` | `profile` |
| Company page | `/company/<slug>` | `company` |
| Job posting | `/jobs/view/<id>` | `job` |
| Jobs hub | `/jobs/...` | `jobs` |
| Messaging | `/messaging/...` | `messaging` |
| My Network | `/mynetwork/...` | `mynetwork` |
| Notifications | `/notifications/...` | `notifications` |
| Search results | `/search/...` | `search` |
| Anything else | — | `other` |

## Tools

### `linkedin_get_page_context`

Cheap probe. Returns `{url, page_type, title, profile_id,
company_slug, job_id, params}`.
