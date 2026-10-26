# Outfit quality and variety

The existing website and mobile browser workflow is unchanged. Open **Suggest**, choose
an occasion, optionally pin a garment and add event details, then generate outfits.
Pinned garments may repeat; other core pieces should vary when suitable alternatives
exist. Accept/reject buttons now refresh the learning profile immediately.

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
