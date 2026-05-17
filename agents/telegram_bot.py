"""
agents/telegram_bot.py

IIM Digest Approval Bot.
Runs as a long-running service on the VPS.

Usage:
  python telegram_bot.py          # polling mode (recommended for VPS)

Systemd service: see /etc/systemd/system/iim-bot.service

Approval flow:
  1. digest_scanner.py sends a numbered list of articles via Telegram
  2. You reply with numbers ("1 3 5"), "all", or "skip"
  3. Bot publishes selected articles directly to GitHub → triggers Cloudflare rebuild

Commands:
  /pending   — show today's pending articles (if any)
  /help      — show this help
"""

import json
import logging
import os
import re
import sys
from datetime import date
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv
from urllib.parse import quote
from github import Github, GithubException
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ── Config ─────────────────────────────────────────────────────────────────────

BOT_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_CHAT = int(os.environ["TELEGRAM_CHAT_ID"])

AGENTS_DIR   = Path(__file__).parent
CONFIG       = yaml.safe_load((AGENTS_DIR / "config.yaml").read_text())
PENDING_DIR  = AGENTS_DIR / "digest"
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO_NAME    = CONFIG.get("github", {}).get("repo", "mattpltn/internetinmyanmar-v2")
BRANCH       = CONFIG.get("github", {}).get("base_branch", "main")
DIGEST_PATH  = "src/content/digest"

VALID_DIGEST_CATEGORIES = {"Shutdown", "Censorship", "Arrest", "Policy", "Data", "Surveillance", "Other"}


def gt_url(url: str) -> str:
    return f"https://translate.google.com/translate?sl=my&tl=en&u={quote(url, safe='')}"


def _translate(text: str, task: str = "translate_mm", max_tokens: int = 200) -> str:
    try:
        sys.path.insert(0, str(AGENTS_DIR))
        from utils.model_router import call
        if task == "translate_mm" and len(text) < 150:
            prompt = "Translate this Myanmar/Burmese title to English. Return only the translated title, nothing else."
        else:
            prompt = "Translate this Myanmar/Burmese text to English. Return only the translation, nothing else."
        return call(task, prompt, content=text, max_tokens=max_tokens).strip()
    except Exception as e:
        log.warning("Translation failed: %s", e)
        return text

_CATEGORY_MAP = {
    "censorship & shutdowns": "Censorship",
    "telecom & infrastructure": "Other",
    "digital economy": "Other",
    "news - mobile": "Other",
    "news - broadband": "Other",
    "news - policy": "Policy",
    "not relevant": "Other",
    "shutdown": "Shutdown",
    "censorship": "Censorship",
    "arrest": "Arrest",
    "policy": "Policy",
    "data": "Data",
    "surveillance": "Surveillance",
}

def normalize_category(raw: str) -> str:
    key = (raw or "").strip().lower()
    if key in _CATEGORY_MAP:
        return _CATEGORY_MAP[key]
    for valid in VALID_DIGEST_CATEGORIES:
        if key == valid.lower():
            return valid
    return "Other"


# ── Auth ───────────────────────────────────────────────────────────────────────

def authorized(update: Update) -> bool:
    return (update.effective_chat is not None
            and update.effective_chat.id == ALLOWED_CHAT)


# ── Pending file helpers ───────────────────────────────────────────────────────

def latest_pending() -> tuple[Path | None, list[dict]]:
    """Return today's pending JSON and its contents (today only — avoids publishing stale articles)."""
    today = date.today().isoformat()
    today_file = PENDING_DIR / f"pending_{today}.json"
    if today_file.exists():
        return today_file, json.loads(today_file.read_text())
    return None, []


def find_candidate_by_slug(slug: str) -> dict | None:
    """Search all pending files (newest first) for a candidate matching slug."""
    files = sorted(PENDING_DIR.glob("pending_*.json"), reverse=True)
    for f in files:
        try:
            candidates = json.loads(f.read_text())
            match = next(
                (c for c in candidates
                 if slugify(c.get("your_title") or c.get("title", "")) == slug),
                None,
            )
            if match:
                return match
        except Exception:
            continue
    return None


# ── MDX builder (mirrors backfill_publisher.py logic) ─────────────────────────

def strip_html(text: str) -> str:
    """Remove HTML tags (including truncated/unclosed ones) and decode entities."""
    import re, html
    # Remove complete tags
    text = re.sub(r"<[^>]+>", "", text)
    # Remove any truncated tag remnant (e.g. "<img alt=..." cut before closing >)
    text = re.sub(r"<[^>]*$", "", text)
    return html.unescape(text).strip()


def make_mdx(c: dict, added_at: str) -> str:
    is_my   = c.get("lang") == "my"
    src_url = c["url"]
    link_url = gt_url(src_url) if is_my else src_url

    title_raw   = c.get("your_title") or c.get("title", "")
    title_safe  = (_translate(title_raw) if is_my else title_raw).replace('"', "'")
    source_safe = c["title"].replace('"', "'")

    tags      = [t.strip() for t in (c.get("tags") or [])]
    tags_yaml = "\n".join([f'  - "{t}"' for t in tags]) if tags else "  []"

    excerpt_raw = strip_html(c.get("summary") or "").strip()
    excerpt = (_translate(excerpt_raw, max_tokens=300) if is_my and excerpt_raw else excerpt_raw)
    if excerpt and not excerpt.endswith((".", "...", "?", "!")):
        excerpt = excerpt.rsplit(" ", 1)[0] + "..."

    source_db_path = AGENTS_DIR / "data" / "source_scores.json"
    source_info: dict = {}
    if source_db_path.exists():
        from urllib.parse import urlparse
        domain = urlparse(src_url).netloc.replace("www.", "")
        db = json.loads(source_db_path.read_text())
        source_info = db.get(domain, {})

    source_score = source_info.get("total", 50)
    source_tier  = source_info.get("tier",  "C")
    source_label = source_info.get("label", "Use with Caution")
    source_name  = source_info.get("name",  c.get("source_name", c.get("source", "")))

    published_at = (c.get("published") or added_at)[:10]

    myanmar_fields = (
        f'\noriginalTitle: "{c["title"].replace(chr(34), chr(39))}"\nsourceLang: "my"'
        if is_my else ""
    )
    featured_image_field = f'\nfeaturedImage: "{c["og_image"]}"' if c.get("og_image") else ""

    return f"""---
title: "{title_safe}"
sourceTitle: "{source_safe}"
source: "{source_name}"
sourceUrl: "{link_url}"
canonical: "{src_url}"
publishedAt: {published_at}
addedAt: {added_at}
category: "{normalize_category(c.get('category', ''))}"
tags:
{tags_yaml}
sourceScore: {source_score}
sourceTier: "{source_tier}"
sourceLabel: "{source_label}"
type: "digest"
draft: false{myanmar_fields}{featured_image_field}
---

*Originally published by [{source_name}]({link_url}) on {published_at}.*

> {excerpt}

[Read the full article on {source_name} →]({link_url})
"""


def slugify(title: str) -> str:
    slug = title.lower()
    slug = slug.encode("ascii", "ignore").decode("ascii")  # strip non-ASCII (safety net)
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[-\s]+", "-", slug)
    return slug[:60].strip("-")


def _english_title(c: dict) -> str:
    """Return the English title for a candidate, translating Burmese if needed. Caches result."""
    if c.get("lang") == "my":
        if not c.get("_translated_title"):
            c["_translated_title"] = _translate(c.get("your_title") or c.get("title", ""))
        return c["_translated_title"]
    return c.get("your_title") or c.get("title", "")


# ── GitHub publish ─────────────────────────────────────────────────────────────

def publish_to_github(selected: list[dict]) -> tuple[int, list[str]]:
    """
    Create MDX files in src/content/digest/ on the main branch via GitHub API.
    Returns (count_published, list_of_filenames).
    """
    g        = Github(GITHUB_TOKEN)
    repo     = g.get_repo(REPO_NAME)
    today    = date.today().isoformat()
    created  = 0
    filenames: list[str] = []

    for c in selected:
        # Best-effort OG image fetch from source article
        if not c.get("og_image"):
            try:
                from distribution.social_poster import fetch_og_image
                og = fetch_og_image(c["url"])
                if og:
                    c["og_image"] = og
                    log.info("OG image found: %s", og)
            except Exception as exc:
                log.warning("OG image fetch failed for %s: %s", c.get("url"), exc)

        pub_date = (c.get("published") or today)[:10]
        title_for_slug = c.get("your_title") or c.get("title", "")
        if c.get("lang") == "my":
            title_for_slug = _translate(title_for_slug)
        slug     = slugify(title_for_slug)
        filename = f"{pub_date}-{slug}.mdx"
        path     = f"{DIGEST_PATH}/{filename}"
        content  = make_mdx(c, today)

        try:
            repo.get_contents(path, ref=BRANCH)
            log.info("Already exists, skipping: %s", filename)
            continue
        except GithubException:
            pass   # doesn't exist yet → create it

        try:
            repo.create_file(
                path,
                f"digest: {c.get('your_title', '')[:60]}",
                content,
                branch=BRANCH,
            )
            created += 1
            filenames.append(filename)
            log.info("Published: %s", filename)
        except Exception as e:
            log.error("Failed to publish %s: %s", filename, e)

    # Persist og_image back into the pending JSON so the social callback can read it
    pending_file, all_candidates = latest_pending()
    if pending_file:
        url_to_og = {c.get("url"): c.get("og_image") for c in selected if c.get("og_image")}
        if url_to_og:
            for candidate in all_candidates:
                og = url_to_og.get(candidate.get("url"))
                if og:
                    candidate["og_image"] = og
            try:
                pending_file.write_text(json.dumps(all_candidates, ensure_ascii=False, indent=2))
            except Exception as exc:
                log.warning("Could not persist og_image to pending file: %s", exc)

    return created, filenames


# ── Handlers ───────────────────────────────────────────────────────────────────

async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    pending_file, candidates = latest_pending()
    if not candidates:
        await update.message.reply_text("No pending articles found.")
        return
    lines = [f"📋 *Pending: {pending_file.stem}*\n"]
    for i, c in enumerate(candidates, 1):
        display_url = gt_url(c["url"]) if c.get("lang") == "my" else c["url"]
        lines.append(
            f"*{i}.* {c.get('your_title', c.get('title', ''))[:70]}\n"
            f"   {display_url}"
        )
    lines.append("\nReply with numbers, `all`, or `skip`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown",
                                     disable_web_page_preview=True)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text(
        "*IIM Digest Bot*\n\n"
        "When the daily scanner sends you a list of articles:\n"
        "• Reply `1 3 5` — publish articles 1, 3 and 5\n"
        "• Reply `all` — publish all articles\n"
        "• Reply `skip` — skip all for today\n\n"
        "/pending — show today's pending list\n"
        "/share <slug> — re-share a published article on Twitter + Facebook\n"
        "/help — this message",
        parse_mode="Markdown"
    )


async def cmd_share(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-share a published digest article: /share <slug-or-partial-slug>"""
    if not authorized(update):
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: `/share <slug>`\n"
            "Example: `/share 2026-04-26-myanmar-junta`",
            parse_mode="Markdown",
        )
        return

    query = " ".join(args).strip().lower()
    article = find_candidate_by_slug(query)

    # If not found by exact slug match, search pending files by partial title
    if not article:
        files = sorted(PENDING_DIR.glob("pending_*.json"), reverse=True)
        for f in files:
            try:
                candidates = json.loads(f.read_text())
                match = next(
                    (c for c in candidates if query in
                     slugify(c.get("your_title") or c.get("title", "")).lower()),
                    None,
                )
                if match:
                    article = match
                    break
            except Exception:
                continue

    if not article:
        await update.message.reply_text(
            f"Could not find article matching `{query}` in pending files.\n"
            "Check the slug and try again.",
            parse_mode="Markdown",
        )
        return

    title = _english_title(article)
    slug  = slugify(title)
    pub_date = (article.get("published") or date.today().isoformat())[:10]
    full_slug = f"{pub_date}-{slug}"

    await update.message.reply_text(f"⏳ Sharing `{full_slug}` on Twitter + Facebook…",
                                     parse_mode="Markdown")
    try:
        from distribution.social_poster import post_all
        results = post_all({
            "title":      title,
            "excerpt":    strip_html(article.get("summary") or "")[:300],
            "category":   normalize_category(article.get("category", "")),
            "source":     article.get("source_name") or article.get("source", ""),
            "slug":       full_slug,
            "source_url": article.get("url", ""),
            "og_image":   article.get("og_image"),
        })
    except Exception as e:
        log.error("Share failed: %s", e)
        await update.message.reply_text(f"❌ Share failed: {e}")
        return

    posted = list(results["posted"].keys())
    errors = results["errors"]
    msg = f"✅ Posted to: {', '.join(posted)}" if posted else "Nothing posted."
    if errors:
        msg += f"\nFailed: {', '.join(f'{p}: {e}' for p, e in errors.items())}"
    await update.message.reply_text(msg)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    text = (update.message.text or "").strip().lower()

    _, candidates = latest_pending()
    if not candidates:
        await update.message.reply_text("No pending articles. Scanner runs at 8 AM.")
        return

    # Parse reply: numbers, "all", or "skip"
    if text == "skip":
        await update.message.reply_text("⏭ Skipped. No articles published today.")
        return

    if text == "all":
        selected = candidates
    else:
        # Extract numbers
        nums = [int(n) for n in re.findall(r"\d+", text)]
        nums = [n for n in nums if 1 <= n <= len(candidates)]
        if not nums:
            await update.message.reply_text(
                "Didn't understand that. Reply with numbers like `1 3 5`, `all`, or `skip`."
            )
            return
        selected = [candidates[n - 1] for n in nums]

    await update.message.reply_text(f"⏳ Publishing {len(selected)} article(s)…")

    try:
        count, filenames = publish_to_github(selected)
    except Exception as e:
        log.error("Publish failed: %s", e)
        await update.message.reply_text(f"❌ Publish failed: {e}")
        return

    if count == 0:
        await update.message.reply_text(
            "⚠️ Nothing new published (articles may already exist)."
        )
        return

    lines = [f"✅ *{count} article(s) published to main*\n"]
    for fn in filenames:
        lines.append(f"• `{fn}`")
    lines.append("\nCloudflare Pages rebuild triggered automatically.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    # Auto-post all published articles to social media
    await update.message.reply_text("📤 Posting to X + Facebook…")
    from distribution.social_poster import post_all
    for article in selected:
        pub_date = (article.get("published") or date.today().isoformat())[:10]
        article_title = _english_title(article)
        article_slug = slugify(article_title)
        try:
            results = post_all({
                "title":      article_title,
                "excerpt":    strip_html(article.get("summary") or "")[:300],
                "category":   normalize_category(article.get("category", "")),
                "source":     article.get("source_name") or article.get("source", ""),
                "slug":       f"{pub_date}-{article_slug}",
                "source_url": article.get("url", ""),
                "og_image":   article.get("og_image"),
            })
            posted = list(results["posted"].keys())
            errors = results["errors"]
            title_short = (article.get("your_title") or article.get("title", ""))[:50]
            msg = f"✅ <b>{title_short}</b> → {', '.join(posted)}" if posted else f"⚠️ Nothing posted for: {title_short}"
            if errors:
                msg += f"\n⚠️ Failed: {', '.join(f'{p}: {str(e)[:80]}' for p, e in errors.items())}"
            await update.message.reply_text(msg, parse_mode="HTML")
        except Exception as e:
            log.error("Auto social post failed for %s: %s", article.get("title"), e)
            await update.message.reply_text(f"⚠️ Social post failed: {e}\nUse /share to retry.")


# ── Social posting callback ────────────────────────────────────────────────────

async def handle_social_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not (query.message.chat and query.message.chat.id == ALLOWED_CHAT):
        return

    data = query.data
    if data.startswith("social_skip:"):
        await query.edit_message_text("Skipped social posting.")
        return

    if data.startswith("social:"):
        slug = data.split(":", 1)[1]

        article = find_candidate_by_slug(slug)
        if not article:
            await query.edit_message_text("Could not find article metadata.")
            return

        await query.edit_message_text("Posting to Twitter + Facebook…")

        try:
            from distribution.social_poster import post_all
            pub_date = (article.get("published") or date.today().isoformat())[:10]
            results = post_all({
                "title":      article.get("your_title") or article.get("title", ""),
                "excerpt":    strip_html(article.get("summary") or "")[:300],
                "category":   article.get("category", ""),
                "source":     article.get("source_name") or article.get("source", ""),
                "slug":       f"{pub_date}-{slug}",
                "source_url": article.get("url", ""),
                "og_image":   article.get("og_image"),
            })
        except Exception as e:
            log.error("Social posting failed: %s", e)
            await query.edit_message_text(f"Failed: {e}")
            return

        posted = list(results["posted"].keys())
        errors = results["errors"]
        msg = f"Posted to: {', '.join(posted)}" if posted else "Nothing posted."
        if errors:
            msg += f"\nFailed: {', '.join(f'{p}: {e}' for p, e in errors.items())}"
        await query.edit_message_text(msg)


# ── Single-instance lock ───────────────────────────────────────────────────────

PID_FILE = Path("/tmp/iim_telegram_bot.pid")

def acquire_lock() -> None:
    """Exit if another instance is already running."""
    if PID_FILE.exists():
        existing_pid = PID_FILE.read_text().strip()
        try:
            os.kill(int(existing_pid), 0)   # signal 0 = existence check only
            log.error("Bot already running (PID %s). Exiting.", existing_pid)
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            log.warning("Stale PID file found (PID %s gone). Overwriting.", existing_pid)
    PID_FILE.write_text(str(os.getpid()))

def release_lock() -> None:
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    acquire_lock()
    try:
        app = Application.builder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("pending", cmd_pending))
        app.add_handler(CommandHandler("help",    cmd_help))
        app.add_handler(CommandHandler("share",   cmd_share))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        app.add_handler(CallbackQueryHandler(handle_social_callback))

        log.info("IIM Digest Bot starting (polling)… PID=%s", os.getpid())
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        release_lock()


if __name__ == "__main__":
    main()
