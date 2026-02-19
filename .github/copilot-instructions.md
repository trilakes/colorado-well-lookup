# Colorado Well Finder — Copilot Instructions

## Project Overview
Colorado Well Finder (coloradowell.com / trilakes.co) is a consumer-facing web tool to search for water well data by address. Users can find wells near any Colorado address, see well depths, flow rates, permits, and environmental hazards. Monetization via Stripe one-time reports.

## Technology Stack
- **Frontend**: Static HTML/CSS/JS on GitHub Pages (trilakes/colorado-well-lookup repo)
- **API Backend**: Render Web Service (`colorado-wells-api`, srv-d67m7hngi27c73a05k9g)
- **Database**: PostgreSQL on Render (colorado-wells-api service)
- **Payments**: Stripe (account acct_1T20bDCSH1YJHcuW, key in MASTER_API_KEYS.env as STRIPE_SECRET_KEY_COWELL)
- **CDN**: Cloudflare (zone: trilakes.co)
- **Analytics**: HQ Dashboard (trilakeshq.com) via inline tracking IIFE

## Key Files
- `index.html` — Main application (5900+ lines, includes map, search, results, paywalls, Stripe checkout, and HQ tracking IIFE)
- `cloudflare-worker.js` — Cloudflare Worker for trilakes.co/blog/* proxying to GitHub Pages

## Deployment
- Push to `trilakes/colorado-well-lookup` master → GitHub Pages auto-deploys
- Domain: coloradowell.com (primary), trilakes.co (Cloudflare)

## ⚠️ RENDER DEPLOYMENT — CRITICAL RULES ⚠️

### NEVER use Render's PUT /env-vars without reading existing vars first!
Render's `PUT /v1/services/{serviceId}/env-vars` **REPLACES ALL env vars**. Always:
1. GET existing env vars first
2. MERGE your changes into the full list
3. PUT the COMPLETE merged list

**Real incident (2026-02-18):** PUT with only one key wiped DATABASE_URL from another service, breaking production during an active ad campaign.

## HQ Tracking Integration
- Inline tracking IIFE in index.html (~250 lines at end of file)
- Posts heartbeats to `https://trilakeshq.com/api/track`
- Tracks: page_load, search_click, results_shown, well_card_click, locked_data_click, paywall_impression, single_click, unlimited_click, hazard_click, fear_popup, overlay_toggle, address_searched, time_milestone, exit_intent_dismissed
- `window.logEvent(action, details)` is exposed globally for external event tracking

## Stripe Pricing
- Single Report: $19 (price_1T2158CSH1YJHcuWdsRuuHyn)
- Property Buyer Pack (10): $49 (price_1T215HCSH1YJHcuWbellDQAu)
- Unlimited Lifetime: $97 (price_1T215RCSH1YJHcuWXTGDhn9O)
