# Shared label definitions (label schema v1)

These are the course's common definitions (GRADING_CONTRACT.md), plus our worked examples and decision rules.
The **same text** is shown to the human labeler (golden 50) and the enrichment and verification models.
Classify the **review text only**. Star rating, likes, app version and date are metadata for analysis. They never substitute for reading the text.

## Topic (exactly one)

| topic | Use for |
|---|---|
| `access` | Login, signup, password, or account access |
| `usability` | Navigation, controls, layout, queue/playlist management, ad interruptions |
| `playback` | Playback failure, crashes, lag, connection failures, audio quality, resource use (battery, storage, data) |
| `downloads` | Downloading, saved music, offline listening, disappearing downloads |
| `catalog` | Missing songs/artists, search/discovery, recommendations, lyrics availability |
| `billing` | Price, charges, subscriptions, paywalls, premium entitlement; explicitly premium-only controls (e.g. "can't pick songs without premium") |
| `support` | Contacting support and the support response |
| `other` | General praise or criticism, unrelated content, or no supported specific topic |

Decision rules:
1. Several problems → choose the one with the **highest supported severity**. On a tie, choose the **first specific problem mentioned**.
2. Positive review → the **first specific praised feature**. General praise ("great app") → `other`.
3. Mentioning a paid plan alone is **not** `billing`. A subscription failing to activate **is** `billing`. Music crashing for a paying customer is `playback`.
4. Ads interrupting listening → `usability`. Being forced to pay to skip or choose songs → `billing` (paywall).

## Intent (exactly one, in precedence order: take the first that applies)

1. `cancellation`: explicitly leaving, uninstalling, cancelling, or threatening to do so ("deleting this app", "cancelling premium").
2. `complaint`: a negative experience, including mixed praise and criticism. A generic "bad app" is a complaint; don't infer a specific defect.
3. `request`: a desired change without a reported failure ("please add a sleep timer").
4. `praise`: positive with no complaint.
5. `unclear`: bare boycott slogans, unrelated or meaningless text, or no product meaning. A boycott becomes a `complaint` or `cancellation` only if a product complaint or explicit personal departure is expressed.

## Severity (integer 1–5)

| severity | Meaning |
|---|---|
| 1 | No reported problem: praise, neutral/unclear content, or a pure feature request |
| 2 | Dislike, generic criticism, minor annoyance, or a cosmetic issue; no supported functional loss |
| 3 | A degraded or restricted function; some use or workaround remains |
| 4 | A clearly blocked core task, such as being unable to log in or play music |
| 5 | Explicit serious financial, privacy, or data harm. An expensive plan, a crash, or angry language alone is **not** enough. |

Cancellation intent does **not** automatically raise severity. Stars, anger and threats don't set severity; the described impact does.

## Sentiment (−1.0 to 1.0)

Overall tone of the text: −1 very negative, 0 neutral or mixed, +1 very positive. Use one decimal place.
Agreement tolerance (declared before evaluation): **±0.3**.

## Evidence quote

An **exact, unmodified substring** of the review that supports the topic and intent. Short reviews may use the whole text. Keep it to the shortest span that justifies the label (at most about 25 words).

## Entities

A list of specific product features or things named in the text (e.g. `"shuffle"`, `"lyrics"`, `"Android Auto"`, `"family plan"`). Empty list if none. Don't add things the text doesn't name.

## needs_review

`true` when the text is ambiguous, lacks the context needed to choose a label, is in a language you can't reliably read, or two labels are equally defensible. Otherwise `false`. Don't invent impact to resolve ambiguity; flag it.

## Worked examples (illustrative, written by us; not dataset quotes and not from the golden set)

| Review | topic | intent | severity | why |
|---|---|---|---|---|
| "Downloaded songs stop playing when I go offline." | downloads | complaint | 4 | Offline playback of saved music is blocked |
| "Too many ads, every two songs. Uninstalling." | usability | cancellation | 2 | Ads = annoyance; cancellation doesn't raise severity |
| "Can't log in since the update, keeps saying wrong password" | access | complaint | 4 | Core task (login) blocked |
| "They charged my card twice this month and won't refund" | billing | complaint | 5 | Explicit financial harm |
| "Songs skip randomly but it mostly works" | playback | complaint | 3 | Degraded, still usable |
| "Love the Discover Weekly playlists!" | catalog | praise | 1 | First specific praised feature = recommendations |
| "Please add a sleep timer on Android" | usability | request | 1 | Pure feature request |
| "Great app" | other | praise | 1 | General praise |
| "Boycott Spotify!!" | other | unclear | 1 | Bare boycott slogan |
| "Premium is worth it, playback is flawless" | playback | praise | 1 | Paid-plan mention alone isn't billing; praised feature is playback |

## Subtopic (our optional extension; one per review, must belong to the chosen topic)

Used only to split complaints into specific issues for ranking. If none fits, use the topic's `other`.

| topic | subtopics |
|---|---|
| access | `login_failure`, `signup`, `password_reset`, `account_locked_or_hacked`, `third_party_login` (Facebook/Google/Apple), `other` |
| usability | `ads_frequency`, `navigation_layout`, `playlist_queue_management`, `ui_redesign`, `controls_widgets`, `other` |
| playback | `crashes`, `stops_or_pauses`, `skipping_glitches`, `buffering_lag`, `connection_errors`, `audio_quality`, `battery_resources`, `device_integration` (car, Bluetooth, watch, casting), `other` |
| downloads | `downloads_disappearing`, `download_failure`, `offline_mode`, `storage`, `other` |
| catalog | `missing_content`, `search`, `recommendations_shuffle`, `lyrics`, `podcasts`, `other` |
| billing | `price`, `charges_refunds`, `premium_only_restrictions` (skip limits, forced shuffle, can't choose songs), `subscription_activation`, `plans_family_student`, `other` |
| support | `no_response`, `unhelpful_response`, `other` |
| other | `general_praise`, `general_criticism`, `unrelated_or_political`, `other` |
