# CLAUDE.md — Internet in Myanmar v2
# Project context — read entirely before any action
# Last updated: 2026-04-28

---

## TOKEN EFFICIENCY — READ THIS FIRST

```
DO:
→ Read only files relevant to the current task
→ Use grep/find to locate files instead of reading directories
→ Write complete files in one shot
→ Chain commands: cmd1 && cmd2 && cmd3

DO NOT:
→ Summarize what you are about to do — just do it
→ Explain what you just did — output speaks for itself
→ Ask questions if the answer is in CLAUDE.md
→ Re-read files already read in this session
→ Read config files (package.json, astro.config) on every session start
```

Python agents: respond with exact output only. No preamble. No markdown fences unless output IS code.
Model selection and token limits live in `agents/config.yaml`.

---

## PROJECT

Site: internetinmyanmar.com — independent technical monitor of Myanmar's digital environment.
Tracking censorship, internet shutdowns, and connectivity for journalists, researchers, and international organizations.

Stack: Astro (SSG + SSR) on Cloudflare Pages. Currently live. WordPress legacy site decommissioned.

---

## TEAM

### Editor-in-Chief — Sacha Nakeo
- **All published articles bylined: Sacha Nakeo** (nom de plume — never real name)
- Speciality: Myanmar, Southeast Asia, digital rights, media freedom
- Languages: French, English, Spanish, Italian
- Author bio: "Sacha Nakeo is a journalist specializing in Myanmar's media landscape, digital rights, and internet freedom. She has closely followed the military junta's systematic censorship of online information since the 2021 coup."
- Tone: precise, analytical, never sensationalist. Credible to both OONI/Citizen Lab and RSF/Freedom House audiences.
- Sacha Nakeo validates all briefs and publishes all articles.

### Technical Director (stays anonymous)
- Manages infrastructure, Claude Code pipeline, VPS agents — not public on site.

---

## MULTILINGUAL STRATEGY

```
English  → primary, all articles first
French   → key investigative pieces — targets RSF, francophone donors
Spanish  → selected pieces — targets FLIP, Spanish-language press freedom orgs
Italian  → occasional — European institutional outreach
```

---

## INFRASTRUCTURE

```
Dev machine:  Windows 11 + WSL2 Ubuntu · repo at ~/dev/iimv2 · dev server localhost:4321
GitHub:       mattpltn/internetinmyanmar-v2
Branches:     main → production · draft/* → per-article PRs
CF Pages:     build: npm run build · output: dist/ · Node 20 · rebuilds on every push to main
VPS:          root@157.180.83.168 · agents at /root/agents/ · logs at /root/logs/
              git repo copy at /root/dev/iimv2 — agents write data here and push to GitHub
SSH key:      ~/.ssh/iim_vps (ed25519, never commit, chmod 600 before use)
MySQL (WP):   VPS 127.0.0.1:3306 · user iim_readonly · SELECT only — NEVER modify WP data
              Local access via SSH tunnel: ssh -i ~/.ssh/iim_vps -L 3307:127.0.0.1:3306 -N -f root@157.180.83.168
```

### Push agents to VPS
```bash
cd ~/dev/iimv2 && tar czf /tmp/agents.tar.gz agents/ \
  && scp -i ~/.ssh/iim_vps /tmp/agents.tar.gz root@157.180.83.168:/tmp/ \
  && ssh -i ~/.ssh/iim_vps root@157.180.83.168 \
       'tar xzf /tmp/agents.tar.gz -C /root/ && rm /tmp/agents.tar.gz' \
  && rm /tmp/agents.tar.gz
```

**WARNING — always `git pull --rebase origin main` before pushing from dev machine.**
The VPS pushes BGP/OONI data commits continuously. If you push without pulling first, the push will be rejected or create divergence. See VPS Git Rules below.

### ~/.bashrc aliases (already set up)
```bash
alias iim-ssh="ssh -i ~/.ssh/iim_vps root@157.180.83.168"
alias iim-push-agents="..."   # compress → scp → decompress
alias iim-tunnel-open="ssh -i ~/.ssh/iim_vps -L 3307:127.0.0.1:3306 -N -f root@157.180.83.168"
alias iim-tunnel-close="pkill -f 'L 3307'"
```

---

## TECH STACK

```
Astro 4.x          SSG + minimal SSR for Observatory live pages
Keystatic CMS      Git-based CMS, browser UI for Sacha Nakeo's validation
Tailwind CSS       Styling
MDX                Article format
Zod                Content schema validation (src/content.config.ts — source of truth)

Python 3.11+       Agent scripts (VPS via cron, venv at /root/agents/venv)
tweepy 4.16+       Twitter/X posting (v1.1 API for media upload, v2 Client for tweets)
httpx              HTTP client for Facebook Graph API calls
requests           HTTP client for OG image fetching
feedparser         RSS parsing
python-telegram-bot Telegram bot (polling mode, systemd service iim-bot)
```

---

## SITE STRUCTURE

```
src/pages/
├── index.astro                   Homepage
├── observatory/
│   ├── index.astro               Data dashboard
│   ├── shutdown-tracker.astro    OONI + CF Radar charts
│   ├── bgp.astro                 BGP network overview
│   ├── bgp/[asn].astro           Per-ASN detail page (reads asn-status.json)
│   ├── blocked-sites.astro
│   └── data.astro                Dataset download page
├── analysis/ · guides/ · news/
└── about/

src/data/                         ← written by VPS agents via git push
├── ooni-history.json             OONI monthly (2021-02 → present)
├── ooni-history-weekly.json      OONI weekly (last 52 weeks)
├── ooni-history-daily.json       OONI daily (last 28 days)
├── cf-traffic.json               CF Radar traffic — monthly + weekly + daily
├── cf-radar-outages.json         Active CF Radar outages for Myanmar
├── bgp-history.json              BGP event history
├── bgp-outages.json              Active/recent BGP outages
├── asn-status.json               Per-ASN current status + timestamp (→ /bgp/[asn] "Last checked")
└── blocked-sites.json
```

---

## CONTENT SCHEMA

Source of truth: `src/content.config.ts`. Key rules:
- `draft: true` always set by agents — only Sacha Nakeo sets `draft: false` via Keystatic
- `author` always `"Sacha Nakeo"`
- Digest collection has `featuredImage: z.string().url().optional()` — set to source article's og:image at publish time

SEO rules enforced by agents:
```
seoTitle:        max 60 chars — primary keyword first
metaDescription: max 155 chars — factual, light CTA
slug:            lowercase, hyphens only, no stop words, max 6 words
alt texts:       descriptive, max 10 words, ZERO keyword stuffing
Internal links:  3-5 per article
```

---

## AGENTS ARCHITECTURE

Developed locally at `~/dev/iimv2/agents/` → deployed to VPS at `/root/agents/`.

```
agents/
├── config.yaml              Models, token limits, sources, paths
├── requirements.txt         pip deps (tweepy, httpx, requests, feedparser, etc.)
├── monitor.py               Daily: fetch sources, score items → monitor_output.json
├── brief_generator.py       Briefs from monitor output
├── writer.py                Full MDX articles from approved briefs
├── publisher.py             Commit MDX + open GitHub PR
├── ooni_watcher.py          OONI + CF Radar → Observatory JSON files
├── bgp_monitor.py           BGP outage watcher → bgp-history.json, bgp-outages.json, asn-status.json
├── bgp_classifier.py        Classifies BGP events (shutdown / incident / noise)
├── digest_scanner.py        Daily digest candidates → digest/pending_YYYY-MM-DD.json
├── telegram_bot.py          Editorial bot: approve digests → auto-publish + auto-post to X + FB
├── process_datasets.py      Data freshness monitoring + Telegram alerts
├── distribution/
│   └── social_poster.py     Post to Twitter/X (tweepy) and Facebook (/photos endpoint)
├── briefs/                  YYYY-MM-DD/[slug].md — awaiting Sacha Nakeo
├── approved/                YYYY-MM-DD/[slug].md — triggers writer.py
└── utils/  (model_router, anthropic_client, github_client, mdx_formatter)
```

### VPS crontab (current)
```
30 6    * * *  monitor.py             → briefs
0  8,20 * * *  ooni_watcher.py        → Observatory JSON
*/5 *   * * *  bgp_monitor.py --critical-only
*/30 *  * * *  bgp_monitor.py         → full run
0  8    * * *  digest_scanner.py
*/30 *  * * *  process_datasets.py    → freshness checks + Telegram alerts
```

### Digest workflow (current)
```
8 AM     digest_scanner.py → Telegram notification with candidate list
Morning  Sacha Nakeo replies with numbers (e.g. "1 3") → bot publishes to GitHub + auto-posts X + FB
         /share <slug> to re-post any article manually
```

---

## OPERATIONAL LESSONS — DO NOT REPEAT

### VPS Git Rules
The VPS pushes data commits continuously (BGP every 5-30 min, OONI 2×/day).
The dev machine also pushes code commits. Both write to the same `main` branch.

**Correct push sequence from dev machine — ALWAYS follow this order:**
```bash
# 1. Stage and commit your changes FIRST
git add <files>
git commit -m "..."

# 2. THEN pull and push
git pull --rebase origin main && git push origin main
```

**NEVER run `git pull --rebase` with unstaged changes present.** It will fail with exit 128.
`git stash` before pull is a fallback but wastes time — just commit first.

**When making hotfixes directly on VPS via SSH:**
- Immediately sync back to local: copy the file, commit locally, push
- Or note the change in session so it isn't overwritten when agents/ is next pushed
- Pushing agents/ from local OVERWRITES all VPS-side changes with no warning

**bgp_monitor.py git pattern** — use `--autostash` to handle other agents' dirty files:
```python
subprocess.run(["git", "-C", repo, "pull", "--rebase", "--autostash"], check=True)
```
Without `--autostash`, any uncommitted file in the repo directory (e.g. from process_datasets.py) causes `git pull --rebase` to fail with exit 128, and commits pile up locally unreleased.

**Symptom of blocked VPS git push:** Observatory pages show stale "Last checked" timestamps.
**Diagnosis:** `git -C /root/dev/iimv2 status` — look for "N commits ahead of origin".
**Fix:** `git -C /root/dev/iimv2 stash && git pull --rebase origin main && git stash pop && git push origin main`

### Telegram Bot — Single Instance
The bot runs as a systemd service (`iim-bot.service`). Never run it manually alongside the service.
If a 409 Conflict error appears in bot.log, kill the orphan process:
```bash
ps aux | grep telegram_bot   # find the non-systemd PID
kill <orphan-pid>
```
Multiple instances cause inline button callbacks (social posting) to be silently dropped.

### Facebook Graph API — Posting with Images
The `/feed` endpoint's `picture` field is **read-only** (returned in GET responses).
To post with an image: use `POST /{page_id}/photos` with `{"url": image_url, "caption": text + "\n\n" + article_url}`.
Without image: use `POST /{page_id}/feed` with `{"message": text, "link": url}`.

### Twitter/X Media Upload
`tweepy.Client` (v2) doesn't upload media. Use `tweepy.API` (v1.1) for upload, then pass `media_ids` to `Client.create_tweet()`. Same OAuth credentials work for both.

### OONI Data Freshness
OONI publishes daily measurements with 24-36h lag. The freshness limit for `ooni-history-daily.json` must be **≥ 49h** (not 25h) to avoid false-positive stale alerts.

### bgp_monitor _status_hash
Always filter non-dict values when reading `asn-status.json` — the file may contain metadata keys:
```python
entries = data if isinstance(data, list) else [v for v in data.values() if isinstance(v, dict)]
```

### VPS Python / pip
The venv does NOT expose `pip3` or `pip` as standalone commands. Always use:
```bash
/root/agents/venv/bin/python3 -m pip install <package>
/root/agents/venv/bin/python3 /path/to/script.py
```

### MailerLite API Integration
- **v3 Connect API** (`connect.mailerlite.com/api`) is broken for campaign creation — PHP `is_array()` validation bug rejects JSON objects for `emails` field. Do not use for campaign creation.
- **v2 legacy API** (`api.mailerlite.com/api/v2`) works for campaign creation. Auth: `X-MailerLite-ApiKey: <key>` header. But group linking in creation payload is unreliable — verify `total_recipients > 0` after creation.
- Once a campaign moves out of "draft" status in v2, it cannot be modified or deleted via API.
- Park MailerLite until the subscriber list is built — revisit when there are active subscribers.

### Cloudflare Workers — No Node.js built-ins
SSR Astro pages (`prerender = false`) run in Cloudflare Workers at runtime. Workers have **no `fs`, `path`, or other Node.js built-ins** unless `nodejs_compat` is enabled in the adapter config (it is NOT currently enabled). A try/catch does NOT protect against this — the `import` itself causes module init failure → hard 500 before any JS runs.

**Rule:** Never use `import { readFileSync } from 'fs'` or `import { resolve } from 'path'` (or any other Node.js built-in) in SSR pages.

**Instead:** Import JSON data files directly via static `import`:
```typescript
import metricsSnapshotRaw from '../../../public/data/metrics_snapshot.json'
```
This works because Vite resolves JSON imports at build time and bundles them into the Worker.

---

## WORDPRESS MIGRATION

### Scoring (wp_scanner.py)
```
40% — Myanmar internet freedom / censorship / digital rights relevance
25% — Telecom / connectivity infrastructure relevance
20% — Quality: >800 words, has sources, substantive
15% — Not outdated / fits new brand

>= 7.0 → MIGRATE · 5-6.9 → FLAG FOR ANNA · < 5.0 → DISCARD
```

### Hard discard
```
→ All crypto articles · All travel articles
→ All "Myanmar Geek" articles unless score >= 8
→ Guest posts from "TurnOnVPN" / "Miss PR" / "Herbert Kanale"
→ Articles under 400 words
```

### HTML → MDX rules
```
→ Strip inline styles, Gutenberg comments, WP shortcodes
→ Rewrite ALL alt texts: descriptive, max 10 words, zero keyword stuffing
→ Set author: "Sacha Nakeo" · Add stale notice for articles > 18 months old
→ Add sources section · Reassign category from approved list
```

### Redirects
```
Migrated:  /old-wp-slug/ → /new-astro-slug  301
Discarded: /old-wp-slug/ → /               301
Output: public/_redirects (Cloudflare Pages format)
```

---

## MCP SERVERS

`.mcp.json` (repo root):
```json
{
  "mcpServers": {
    "github":   { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                  "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}" } },
    "fetch":    { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-fetch"] },
    "mysql":    { "command": "npx", "args": ["-y", "@benborla29/mcp-server-mysql"],
                  "env": { "MYSQL_HOST": "127.0.0.1", "MYSQL_PORT": "3307",
                            "MYSQL_USER": "iim_readonly", "MYSQL_PASS": "${WP_DB_PASSWORD_READONLY}",
                            "MYSQL_DB": "${WP_DB_NAME}",
                            "ALLOW_INSERT_OPERATION": "false", "ALLOW_UPDATE_OPERATION": "false",
                            "ALLOW_DELETE_OPERATION": "false" } },
    "sequential-thinking": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"] }
  }
}
```

`.env` keys (never commit): `ANTHROPIC_API_KEY` · `GITHUB_TOKEN` · `WP_DB_NAME` · `WP_DB_PASSWORD_READONLY` · `CLOUDFLARE_API_TOKEN` · `CLOUDFLARE_ACCOUNT_ID`

---

## ABSOLUTE RULES — NEVER VIOLATE

```
→ draft: false is set only by Sacha Nakeo — never by agents or Claude Code
→ Author is always "Sacha Nakeo"
→ Never commit .env or any file with secrets
→ Never push to main without git pull --rebase first (VPS pushes continuously)
→ Never push agents/ to VPS without checking for VPS-side hotfixes first
→ Never write keyword-stuffed alt texts
→ Never modify the WordPress database — SELECT only
→ Never auto-publish without a Sacha Nakeo-approved brief
→ Never migrate crypto or travel articles
→ Newsletter from this pipeline is sent to 'test' group only — never full list
→ GitHub PRs from skills are always draft — never auto-merged
```
