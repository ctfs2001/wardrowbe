# Outfit quality and variety

The existing website and mobile browser workflow is unchanged. Open **Suggest**, choose
an occasion, optionally pin a garment and add event details, then generate outfits.
Pinned garments may repeat; other core pieces should vary when suitable alternatives
exist. Accept/reject buttons now refresh the learning profile immediately.

## Planning the wearing time

The suggestion page offers today/tomorrow and morning (06–12), afternoon (12–18),
evening (18–24), or full day (06–24), in the account time zone. Both the preview
and generation use the same selected hourly forecast window from the existing
[Open-Meteo API](https://open-meteo.com/en/docs). Average temperature/apparent
temperature, the selected temperature range, maximum rain probability and maximum
wind guide the recommendation. Missing forecast hours produce an error, never a
silent fallback to current weather. A manual weather override takes priority.

Optional quick needs cover air-conditioned interiors, outdoor use, walking,
rain-friendly materials, movement and a more polished look. They guide styling,
not the measured weather, and are saved alongside free-text event notes. The
outfit uses the selected wearing date and period; cache contexts isolate dates
and periods. Requests without a wearing day retain legacy current-weather behavior.
No database migration is needed. This planning change awaits review and deployment.

## Explicit reactions and recommendation memory

The website uses three actions per look: Accept (worn today and liked), Like
(preference only), and Dislike (negative preference only). Unanswered/skipped looks
supply no preference signal. Other options stay available after a reaction. Future
looks can be liked now and accepted on the wearing day. Accept records a single
wear per outfit, including concurrent retries; changing a preference preserves the
wear record. The worn filter checks `feedback.worn_at`, including bulk selection.
Legacy `/accept` clients keep their original preference-only semantics.

Recommendations read at most 200 explicit responses from the last 180 days, with a
30-day half-life. Same-core variations on the same day count once. Inferred
color/style preference bonuses remain bounded (up to 0.15 combined), as does the
optional pairing bonus (up to 0.15); neither is a blacklist. Rejecting a look does
not imply disliking all its colors or garments. One suitable exploratory option is
encouraged without relaxing quality, weather, or daily accessory requirements.
Existing exposure rotation remains active. Daily cache context expiry makes decay
effective without another feedback click. Lifetime analytics and records are kept;
only recommendation-time memory is bounded. This change awaits review/deployment.

## Fewer options than requested

`/outfits/suggest-options` still asks for three looks and still returns a plain JSON
array, so older clients keep working. Each element now also carries an optional
`suggestion_batch` object (`id`, `target_count`, `saved_count`, `missing_count`,
`can_fill`, `filling`, `expired`, `fill_attempts_remaining`). When fewer than three
looks pass the checks, the suggest page says "2 of 3" and offers **Complete the
remaining outfits**. Quality rules are not relaxed to reach the count.

- `POST /outfits/suggestion-batches/{id}/fill` generates only the missing looks from
  the batch's saved request snapshot: wearing date, period, the weather used for the
  original request (no new forecast), occasion, quick needs, free-text notes, pinned
  and excluded items. New looks pass the same completeness, accessory, formality and
  repeat checks and must differ in core pieces from the looks already kept; pinned
  items may still repeat. Existing looks, their IDs, content and reactions are never
  modified. `GET /outfits/suggestion-batches/{id}` returns the current state.
- Repeats are safe: a PostgreSQL advisory lock per batch lets one fill run; a
  concurrent request gets `409 fill_in_progress` without calling the AI, and a retry
  after success returns the complete batch without calling it again. The page
  disables the button while filling and, after a timeout or 409, polls the batch.
- Errors carry `detail.code`: `ai_unavailable`, `fill_no_valid_option`,
  `batch_expired` (wearing date passed), `fill_limit_reached`, `request_invalid`
  (e.g. a pinned item was archived), `batch_not_found`. Existing looks stay on screen.
- The snapshot lives under `ai_raw_response["_batch"]` of each saved look, so no
  migration is needed.

AI call budget: a normal request makes one call, plus at most one repair when fewer
than three looks pass. Each user-initiated fill makes at most two calls (generation
plus one repair) and asks only for the missing count. At most three fills per batch
reach the AI (Redis counter, two-day TTL; if Redis is unavailable the counter fails
open like the existing rate limiter, and the shared 10-per-minute suggest limit
still applies). There are no automatic or unbounded retries. Repair and fill prompts
now ask for exactly the number still needed instead of repeating "exactly 3".

### Diagnostics

Every generation logs `recommendation_attempt` (per AI call) and
`recommendation_result` (per request) as single-line JSON sharing a `request_id`,
which equals the batch id for multi-option requests and fills. Records contain
counts, the model name and stable reason codes only, never prompts, replies,
notes, item descriptions or credentials. The request summary is also stored with
the saved looks under `_batch.diagnostics`.

| Code | Meaning |
|---|---|
| `model_short_response` | The model returned fewer options than requested |
| `parse_error` | The reply was not usable JSON |
| `invalid_format` | Items were not a list of integer wardrobe numbers |
| `unavailable_item` | A number outside the available candidates |
| `missing_mandatory` | A pinned item was left out |
| `incomplete_outfit` | No top/bottom or full-body garment |
| `footwear_count` | Not exactly one pair of shoes |
| `slot_conflict` | Two pieces occupy the same body slot |
| `hot_weather_legwear` | Unrequested legwear in consistently hot weather |
| `formality_mismatch` | Extreme formality gap between core pieces and shoes |
| `missing_daily_accessory` | Eyewear, hat or bag missing although available |
| `duplicate_in_batch` | Repeats the core of another look in the same batch |
| `duplicate_recent` | Repeats the core of a recently suggested look |
| `reused_key_piece` | Reuses a key piece while suitable unused ones remain |
| `ai_call_failed` / `ai_response_truncated` | The provider call failed or returned no content |

An unparsable reply now logs only its length at WARNING level; the full text is
logged at DEBUG only, because it can echo private event notes.

## Changes

- Rank every wardrobe size. Count recommendations in the last seven days separately
  from actual wear, with a bounded penalty for frequently suggested core garments.
  Shoes/accessories and pinned items are exempt from that penalty.
- Rejecting a look does not ban every garment in it. Recently suggested core
  combinations (last 12 hours, up to ten looks) are avoided, even if accessories change.
- Validate selected inventory IDs, body coverage, one pair of shoes, incompatible
  body slots, required pieces, hot-weather legwear and extreme known formality gaps.
  Compare the options for repeated key pieces when unused suitable alternatives exist.
- Validate before saving; do not silently add a different shoe and leave an explanation
  about the original look. Cache entries use the same checks and a versioned context.
- If needed, make **at most one repair call to the existing AI provider**. This can add
  latency and token use. Return fewer valid options if repair fails; never fill the
  result with invalid outfits. No valid option produces an actionable error. A short
  result is reported to the page, which can fill only the gap (see above).
- Whole-look rejection supplies no negative color/style signal. Positive feature scores
  count once per look and shrink toward neutral with limited evidence. Explicit user
  color exclusions still apply. Ignore legacy inferred color dislikes in recommendations.
- Require exactly one hat, bag and eyewear item per look when that category exists in
  available inventory, alongside exactly one footwear item. All four may repeat daily
  and across options. Eyewear is identified by the accessories subtype; jewelry does
  not substitute for glasses. Missing categories are omitted (footwear remains required).
  Preserve these categories in the capped candidate list. Generated and cached options
  share validation; cache quality version is 3. This change awaits deployment.
- Relax the blanket ban on wide tops with wide trousers, and
  require explanations tied to the selected garments' known details.

## Deployment and limits

No database migration, new API provider or new secret is required. The current provider
still generates outfits; this does not move the website's inference into ChatGPT.
Old profile displays update on the next accept/reject or the existing learning recompute
action. Existing wardrobe records and photographs are preserved.

Quality checks rely on the stored clothing tags. They cannot guarantee subjective taste
or detect incorrect fabric/fit tags from photographs. A wardrobe with too few distinct
core combinations may return fewer options or require different selections.

This branch starts at `e88463c` (which already contains optional product-photo redraw).
Redraw defaults to **off**; the NAS deployment keeps it explicitly off. Deployment is
through the existing nas-infra PR workflow. Roll back by restoring the previous
`SOURCE_REF` through that same workflow.

## Validation

Run the backend suite with an isolated PostgreSQL database and Redis instance, setting
both `DATABASE_URL` and `TEST_DATABASE_URL` to that test database. Never use the live
wardrobe database. The quality tests mock AI responses, including duplicate tops,
incomplete outfits, repair failures and rejected combinations; they make no paid calls.
