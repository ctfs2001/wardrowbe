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
  result with invalid outfits. No valid option produces an actionable error.
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
