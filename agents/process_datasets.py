"""
process_datasets.py — Unified Data Layer Orchestrator
======================================================
Reads all raw poller outputs from src/data/, joins and enriches them,
and writes public/data/ outputs. Run by cron 15 min after ooni_watcher.py.

Data flow:
  bgp-outages.json      ──► normalize_bgp_outages()  ──┐
  keepiton-shutdowns.json ► normalize_keepiton()      ──┼─► join_unified_events()
  cf-radar-outages.json ──► normalize_cf_radar()      ──┘        │
                                                                  ▼
  ooni-history-daily.json ──► compute_metrics()  unified_events.json
  blocked-sites.json       ──► compute_metrics()  metrics_snapshot.json
  asn-status.json          ──► compute_metrics()  bgp_events.csv
  cf-traffic.json          ──► compute_metrics()  keepiton_shutdowns.csv
                                                  ooni_timeseries.csv
                                                  blocked_sites.csv

Architecture principles followed:
- SSoT: only this script reads src/data/* and writes public/data/*
- Raw → Derived separation: source files are never modified
- Idempotent: same inputs always produce identical outputs
- Extensible: new sources → new normalize_*() + extend join_unified_events()
- Static-first: outputs are plain JSON + CSV, no runtime DB required
- Observable: every output contains generated_at + dataset_version + processing_log

Usage:
  python process_datasets.py           # full run → push to GitHub
  python process_datasets.py --dry-run # process + print stats, no push

Cron (VPS, after ooni_watcher.py):
  15 8  * * * ~/agents/venv/bin/python ~/agents/process_datasets.py >> ~/logs/process_datasets.log 2>&1
  15 20 * * * ~/agents/venv/bin/python ~/agents/process_datasets.py >> ~/logs/process_datasets.log 2>&1
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------

AGENTS_DIR  = Path(__file__).parent
# DATA_PATH env var mirrors the convention in bgp_monitor.py and ooni_watcher.py.
# On VPS: DATA_PATH=/root/dev/iimv2/src/data (set in .env)
# Locally: falls back to relative path from agents/ parent.
_data_path_env = os.environ.get("DATA_PATH", "")
if _data_path_env:
    DATA_DIR  = Path(_data_path_env)
    REPO_ROOT = DATA_DIR.parent.parent   # …/src/data → …/
else:
    REPO_ROOT = AGENTS_DIR.parent
    DATA_DIR  = REPO_ROOT / "src" / "data"
OUTPUT_DIR  = REPO_ROOT / "public" / "data"

CONFIG      = yaml.safe_load((AGENTS_DIR / "config.yaml").read_text())
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO_NAME   = CONFIG["github"]["repo"]

# Output file paths (relative to repo root, for GitHub API)
OUTPUT_FILES = {
    "unified_events":    "public/data/unified_events.json",
    "metrics_snapshot":  "public/data/metrics_snapshot.json",
    "bgp_events":        "public/data/bgp_events.csv",
    "keepiton_shutdowns": "public/data/keepiton_shutdowns.csv",
    "ooni_timeseries":   "public/data/ooni_timeseries.csv",
    "blocked_sites":     "public/data/blocked_sites.csv",
}

# Freshness thresholds: how old can a source file be before we warn?
FRESHNESS_LIMITS = {
    "bgp-outages.json":          timedelta(hours=3),
    "keepiton-shutdowns.json":   timedelta(days=8),
    "ooni-history-daily.json":   timedelta(hours=25),
    "blocked-sites.json":        timedelta(hours=25),
    "cf-traffic.json":           timedelta(hours=25),
    "asn-status.json":           timedelta(hours=1),
    "cf-radar-outages.json":     timedelta(hours=25),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def dataset_version(sources: dict) -> str:
    """
    Derive version from the most recent lastUpdated/timestamp across all sources.
    Deterministic: same input files → same version string, regardless of run time.
    Falls back to wall-clock only if no source timestamps are parseable.
    """
    timestamps: list[datetime] = []
    for raw in sources.values():
        candidate: str | None = None
        if isinstance(raw, dict):
            candidate = raw.get("lastUpdated") or raw.get("timestamp")
        elif isinstance(raw, list) and raw:
            last = raw[-1]
            candidate = (last.get("timestamp") or last.get("started_at")
                         or last.get("measurement_start_day"))
        if candidate:
            try:
                timestamps.append(parse_dt(candidate if "T" in candidate
                                           else candidate + "T00:00:00Z"))
            except Exception:
                pass
    newest = max(timestamps) if timestamps else now_utc()
    return f"v{newest.strftime('%Y%m%d%H%M')}"


def _content_changed(existing: str, new: str, is_json: bool) -> bool:
    """
    Compare file contents, ignoring the generated_at/dataset_version header so
    that files with identical data don't generate spurious commits on each run.
    """
    if is_json:
        try:
            a = json.loads(existing)
            b = json.loads(new)
            for key in ("generated_at", "dataset_version"):
                a.pop(key, None)
                b.pop(key, None)
            return a != b
        except Exception:
            pass
    else:
        # CSVs: skip provenance comment lines starting with #
        def strip_comments(s: str) -> list[str]:
            return [l for l in s.splitlines() if not l.startswith("#")]
        return strip_comments(existing) != strip_comments(new)
    return existing.strip() != new.strip()


def _telegram_alert(message: str) -> None:
    """Send a plain-text alert to the configured Telegram chat (sync, best-effort)."""
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning("[TELEGRAM DISABLED] %s", message[:120])
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown",
                  "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as exc:
        log.error("Telegram alert failed: %s", exc)


def parse_dt(raw: str) -> datetime:
    """Parse ISO 8601 string (with or without timezone) → UTC datetime."""
    raw = raw.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_date(raw: str | None) -> date | None:
    """Parse YYYY-MM-DD string → date object, or None."""
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def severity_bgp(duration_minutes: int, ioda_confirmed: bool, min_visibility: float) -> int:
    """1–5 severity for a BGP outage."""
    if ioda_confirmed:
        return 5 if duration_minutes > 240 else 4
    if duration_minutes > 240:
        return 3
    if duration_minutes > 30:
        return 2
    return 1


def severity_keepiton(shutdown_type: str, scope: str, ongoing: bool) -> int:
    """1–5 severity for a KeepItOn shutdown."""
    nationwide = any(w in scope.lower() for w in ("nationwide", "national", "countrywide", "myanmar"))
    if shutdown_type == "full_network":
        if nationwide or ongoing:
            return 5
        return 4
    if shutdown_type == "mobile":
        return 3
    if shutdown_type == "throttle":
        return 2
    return 2  # platform blocks


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load_raw_sources() -> tuple[dict, list[str]]:
    """
    Load all source JSON files from src/data/.
    Returns (sources dict, warnings list).
    Partial failure is allowed: missing files produce empty defaults.
    """
    sources: dict[str, Any] = {}
    warnings: list[str] = []

    files = {
        "bgp_outages":    "bgp-outages.json",
        "bgp_status":     "asn-status.json",
        "keepiton":       "keepiton-shutdowns.json",
        "ooni_daily":     "ooni-history-daily.json",
        "ooni_monthly":   "ooni-history.json",
        "blocked_sites":  "blocked-sites.json",
        "cf_traffic":     "cf-traffic.json",
        "cf_radar":       "cf-radar-outages.json",
    }

    for key, filename in files.items():
        path = DATA_DIR / filename
        try:
            data = json.loads(path.read_text())
            sources[key] = data

            # Freshness check using lastUpdated or top-level timestamp
            limit = FRESHNESS_LIMITS.get(filename)
            if limit:
                last_updated_str = None
                if isinstance(data, dict):
                    last_updated_str = data.get("lastUpdated") or data.get("timestamp")
                elif isinstance(data, list) and data:
                    last_updated_str = (data[-1].get("timestamp")
                                        or data[-1].get("started_at")
                                        or data[-1].get("measurement_start_day"))
                if last_updated_str:
                    try:
                        age = now_utc() - parse_dt(last_updated_str)
                        if age > limit:
                            warnings.append(f"{filename} is stale ({age} old, limit {limit})")
                    except Exception:
                        pass  # can't parse timestamp — skip freshness check

        except FileNotFoundError:
            sources[key] = {} if key != "bgp_outages" else []
            warnings.append(f"Missing: {filename} — outputs will be partial")
        except Exception as exc:
            sources[key] = {} if key != "bgp_outages" else []
            warnings.append(f"Failed to load {filename}: {exc}")

    log.info(
        "Loaded sources: bgp_outages=%s keepiton=%s ooni_daily=%s blocked_sites=%s",
        len(sources.get("bgp_outages") or []),
        len((sources.get("keepiton") or {}).get("events", [])),
        len(sources.get("ooni_daily") or []),
        len((sources.get("blocked_sites") or {}).get("sites", [])),
    )
    return sources, warnings


# ---------------------------------------------------------------------------
# Normalize
# ---------------------------------------------------------------------------

def normalize_bgp_outages(raw: list[dict]) -> list[dict]:
    """
    Normalize BGP outage records.
    Input:  bgp-outages.json array
    Output: list of normalized event dicts with UTC datetimes as strings
    """
    normalized = []
    for rec in raw:
        try:
            started_at = parse_dt(rec["started_at"])
            ended_at   = parse_dt(rec["ended_at"]) if rec.get("ended_at") else None
            duration   = int(rec.get("duration_minutes", 0))
            ioda       = bool(rec.get("ioda_confirmed", False))
            vis        = float(rec.get("min_visibility_pct", 0.0))

            normalized.append({
                "asn":               rec.get("asn", ""),
                "isp_name":          rec.get("name", ""),
                "started_at":        iso(started_at),
                "ended_at":          iso(ended_at) if ended_at else None,
                "duration_minutes":  duration,
                "min_visibility_pct": vis,
                "ioda_confirmed":    ioda,
                "resolved":          bool(rec.get("resolved", True)),
                "_started_dt":       started_at,   # internal — stripped before export
                "_ended_dt":         ended_at,
            })
        except Exception as exc:
            log.debug("Skipping malformed BGP record: %s — %s", rec.get("asn"), exc)

    normalized.sort(key=lambda r: r["_started_dt"])
    log.info("Normalized %d BGP outages", len(normalized))
    return normalized


def normalize_keepiton(raw: dict) -> list[dict]:
    """
    Normalize KeepItOn shutdown records.
    Input:  keepiton-shutdowns.json wrapper object
    Output: list of normalized event dicts
    """
    events = raw.get("events", [])
    normalized = []
    today = date.today()

    for rec in events:
        start = parse_date(rec.get("startDate"))
        if not start:
            continue

        ongoing = bool(rec.get("ongoing", False))
        end_raw = parse_date(rec.get("endDate"))
        end     = today if (ongoing and end_raw is None) else end_raw

        duration_days = (end - start).days if end else None

        normalized.append({
            "id":            rec.get("id", ""),
            "start_date":    start.isoformat(),
            "end_date":      end.isoformat() if end else None,
            "ongoing":       ongoing,
            "duration_days": duration_days,
            "type":          rec.get("type", "full_network"),
            "scope":         rec.get("scope", ""),
            "perpetrator":   rec.get("perpetrator", ""),
            "services":      rec.get("services", []),
            "source_url":    rec.get("sourceUrl", ""),
            "_start_date":   start,   # internal
            "_end_date":     end,
        })

    normalized.sort(key=lambda r: r["_start_date"])
    log.info("Normalized %d KeepItOn events (%d ongoing)", len(normalized),
             sum(1 for r in normalized if r["ongoing"]))
    return normalized


def normalize_cf_radar(raw: dict) -> list[dict]:
    """
    Normalize CF Radar outage records.
    Currently often empty; designed for future data.
    """
    outages = raw.get("activeOutages", []) + raw.get("recentOutages", [])
    normalized = []
    for rec in outages:
        try:
            start_raw = rec.get("start") or rec.get("started_at") or ""
            end_raw   = rec.get("end")   or rec.get("ended_at")
            started_at = iso(parse_dt(start_raw)) if start_raw else ""
            ended_at   = iso(parse_dt(end_raw))   if end_raw   else None
            normalized.append({
                "started_at": started_at,
                "ended_at":   ended_at,
                "scope":      rec.get("scope", ""),
                "type":       rec.get("type", "outage"),
            })
        except Exception:
            pass
    return normalized


# ---------------------------------------------------------------------------
# Join
# ---------------------------------------------------------------------------

def _keepiton_active_on(kit_events: list[dict], dt: datetime) -> list[dict]:
    """Return KeepItOn events active on the given datetime's date."""
    d = dt.date()
    return [
        k for k in kit_events
        if k["_start_date"] <= d and (k["_end_date"] is None or k["_end_date"] >= d)
    ]


def join_unified_events(
    bgp: list[dict],
    keepiton: list[dict],
    cf_radar: list[dict],
    **extra_sources: list[dict],
) -> list[dict]:
    """
    Produce the unified events list.

    Strategy:
    - BGP outage events → one event per outage, enriched with KeepItOn
      context if the outage falls within an active KIT shutdown period
    - KeepItOn shutdown events → one event per KIT record, enriched with
      count of concurrent BGP outages
    - CF Radar events → included when present

    Cross-validation:
    A BGP outage is "keepiton_matched" when its started_at date falls
    within the [start_date, end_date] window of any active KIT shutdown.
    Note: since 72/95 KIT events are ongoing post-coup regional shutdowns,
    most BGP outages will match. The match is a temporal correlation, not
    geographic confirmation (ASN → region mapping not yet available).

    Extensibility: pass new normalized sources as keyword args, e.g.
      join_unified_events(bgp, kit, cf, ioda=ioda_events)
    Each extra source should be a list of dicts with at least event_time,
    event_type, sources, severity, is_confirmed, and metadata fields.
    They are appended verbatim after the built-in sources.
    """
    events: list[dict] = []
    seq = [0]

    def next_id(prefix: str) -> str:
        seq[0] += 1
        return f"{prefix}-{seq[0]:04d}"

    # ── BGP outage events ──────────────────────────────────────────────────
    for bgp_ev in bgp:
        dt = bgp_ev["_started_dt"]
        matched_kit = _keepiton_active_on(keepiton, dt)

        sev = severity_bgp(
            bgp_ev["duration_minutes"],
            bgp_ev["ioda_confirmed"],
            bgp_ev["min_visibility_pct"],
        )
        if matched_kit:
            sev = min(5, sev + 1)

        events.append({
            "event_id":          next_id("bgp"),
            "event_time":        bgp_ev["started_at"],
            "event_end":         bgp_ev["ended_at"],
            "duration_minutes":  bgp_ev["duration_minutes"],
            "event_type":        "outage",
            "sources":           ["bgp"],
            "asn":               bgp_ev["asn"],
            "isp_name":          bgp_ev["isp_name"],
            "severity":          sev,
            "scope":             None,
            "perpetrator":       None,
            "affected_services": [],
            "is_confirmed":      bgp_ev["ioda_confirmed"],
            "keepiton_matched":  bool(matched_kit),
            "keepiton_ids":      [k["id"] for k in matched_kit if k["id"]],
            "source_urls":       [],
            "metadata":          {
                "min_visibility_pct": bgp_ev["min_visibility_pct"],
                "resolved":           bgp_ev["resolved"],
                "ioda_confirmed":     bgp_ev["ioda_confirmed"],   # BGP-specific signal
            },
        })

    # ── KeepItOn shutdown events ───────────────────────────────────────────
    for kit_ev in keepiton:
        start_dt = datetime.combine(kit_ev["_start_date"], datetime.min.time()).replace(
            tzinfo=timezone.utc
        )
        end_d    = kit_ev["_end_date"]
        end_dt   = (
            datetime.combine(end_d, datetime.max.time()).replace(tzinfo=timezone.utc)
            if end_d else None
        )

        concurrent_bgp = [
            b for b in bgp
            if (
                b["_started_dt"] >= start_dt
                and (end_dt is None or b["_started_dt"] <= end_dt)
            )
        ]

        sev = severity_keepiton(kit_ev["type"], kit_ev["scope"], kit_ev["ongoing"])
        if concurrent_bgp:
            sev = min(5, sev + 1)

        events.append({
            "event_id":          next_id("kit"),
            "event_time":        f"{kit_ev['start_date']}T00:00:00Z",
            "event_end":         f"{kit_ev['end_date']}T23:59:59Z" if kit_ev["end_date"] else None,
            "duration_minutes":  (kit_ev["duration_days"] or 0) * 1440,
            "event_type":        "shutdown",
            "sources":           ["keepiton"],
            "asn":               None,
            "isp_name":          None,
            "severity":          sev,
            "scope":             kit_ev["scope"],
            "perpetrator":       kit_ev["perpetrator"],
            "affected_services": kit_ev["services"],
            "is_confirmed":      True,   # human-verified by Access Now
            "keepiton_matched":  False,
            "keepiton_ids":      [kit_ev["id"]] if kit_ev["id"] else [],
            "source_urls":       [kit_ev["source_url"]] if kit_ev["source_url"] else [],
            "metadata":          {
                "kit_type":          kit_ev["type"],
                "ongoing":           kit_ev["ongoing"],
                "duration_days":     kit_ev["duration_days"],
                "concurrent_bgp":    len(concurrent_bgp),
            },
        })

    # ── CF Radar events ────────────────────────────────────────────────────
    for cf_ev in cf_radar:
        events.append({
            "event_id":          next_id("cf"),
            "event_time":        cf_ev.get("started_at", ""),
            "event_end":         cf_ev.get("ended_at"),
            "duration_minutes":  None,
            "event_type":        "outage",
            "sources":           ["cloudflare"],
            "asn":               None,
            "isp_name":          None,
            "severity":          3,
            "scope":             cf_ev.get("scope"),
            "perpetrator":       None,
            "affected_services": [],
            "is_confirmed":      True,
            "keepiton_matched":  False,
            "keepiton_ids":      [],
            "source_urls":       [],
            "metadata":          {},
        })

    # ── Extra sources (future: ioda, user_submissions, …) ─────────────────
    for source_name, source_events in extra_sources.items():
        for ev in (source_events or []):
            ev.setdefault("event_id", next_id(source_name[:4]))
            events.append(ev)

    # Sort chronologically
    def sort_key(e: dict) -> str:
        return e.get("event_time") or ""

    events.sort(key=sort_key)
    log.info(
        "Unified events: %d total (%d BGP outages, %d KIT shutdowns, %d CF)",
        len(events),
        sum(1 for e in events if "bgp" in e["sources"]),
        sum(1 for e in events if "keepiton" in e["sources"]),
        sum(1 for e in events if "cloudflare" in e["sources"]),
    )
    return events


# ---------------------------------------------------------------------------
# Derived indicators
# ---------------------------------------------------------------------------

def compute_metrics(
    unified_events: list[dict],
    ooni_daily: list[dict],
    blocked_sites: dict,
    cf_traffic: dict,
    bgp_status: dict,
) -> dict:
    """
    Compute pre-aggregated metrics for the metrics_snapshot.json.
    All values are safe to embed directly in Astro components.
    """
    today = date.today()

    # Active shutdowns: KeepItOn events still ongoing
    active_shutdowns = sum(
        1 for e in unified_events
        if e["event_type"] == "shutdown" and e["metadata"].get("ongoing", False)
    )

    # Total KIT shutdown events
    total_shutdowns = sum(1 for e in unified_events if e["event_type"] == "shutdown")

    # Total BGP outage events
    total_bgp = sum(1 for e in unified_events if e["event_type"] == "outage"
                    and "bgp" in e["sources"])

    # Blocked domains
    confirmed_blocked = int((blocked_sites or {}).get("totalDomains", 0))

    # OONI anomaly rate: average of last 30 daily records
    ooni_rate_30d = None
    if ooni_daily:
        rates = [r["anomaly_rate"] for r in ooni_daily if r.get("anomaly_rate") is not None]
        ooni_rate_30d = round(sum(rates) / len(rates), 1) if rates else None

    # Censorship prevalence: most recent day's anomaly rate
    prevalence_pct = None
    if ooni_daily:
        last = sorted(ooni_daily, key=lambda r: r.get("period", ""))[-1]
        prevalence_pct = last.get("anomaly_rate")

    # BGP networks down right now
    bgp_down_now = sum(
        1 for asn_data in (bgp_status or {}).values()
        if isinstance(asn_data, dict) and asn_data.get("status") in ("RED", "YELLOW")
    )

    # Days since last high-severity event
    high_sev = [
        e for e in unified_events
        if e.get("severity", 0) >= 4 and e.get("event_time")
    ]
    days_since_major = None
    if high_sev:
        last_major = max(high_sev, key=lambda e: e["event_time"])
        try:
            last_dt = parse_dt(last_major["event_time"])
            days_since_major = (now_utc() - last_dt).days
        except Exception:
            pass

    # Shutdown impact days total (KeepItOn events with duration)
    impact_days = 0
    for e in unified_events:
        if e["event_type"] == "shutdown":
            dd = e["metadata"].get("duration_days")
            if dd is not None:
                impact_days += int(dd)

    # CF traffic latest
    cf_latest = None
    if cf_traffic and cf_traffic.get("daily"):
        daily_sorted = sorted(cf_traffic["daily"], key=lambda r: r.get("timestamp", ""))
        if daily_sorted:
            cf_latest = daily_sorted[-1].get("cf_traffic")

    return {
        "active_shutdowns":         active_shutdowns,
        "total_shutdown_events":    total_shutdowns,
        "total_bgp_outage_events":  total_bgp,
        "confirmed_blocked_sites":  confirmed_blocked,
        "ooni_anomaly_rate_30d":    ooni_rate_30d,
        "censorship_prevalence_pct": prevalence_pct,
        "bgp_networks_down_now":    bgp_down_now,
        "days_since_major_outage":  days_since_major,
        "shutdown_impact_days_total": impact_days,
        "cf_traffic_latest_pct":    cf_latest,
    }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def make_header(version: str, log_data: dict) -> dict:
    return {
        "generated_at":   iso(now_utc()),
        "dataset_version": version,
        "processing_log": log_data,
    }


def export_json(path: Path, header: dict, data: Any) -> str:
    """Return JSON string (not written to disk — caller decides)."""
    payload = {**header, "events" if isinstance(data, list) else "data": data}
    # Special case for metrics_snapshot — flat structure
    if isinstance(data, dict):
        payload = {**header, **data}
    return json.dumps(payload, indent=2, ensure_ascii=False)


def export_metrics_json(path: Path, header: dict, metrics: dict) -> str:
    payload = {**header, **metrics}
    return json.dumps(payload, indent=2, ensure_ascii=False)


def records_to_csv(rows: list[dict], fieldnames: list[str],
                   generated_at: str = "", dataset_version: str = "") -> str:
    buf = io.StringIO()
    if generated_at:
        buf.write(f"# generated_at: {generated_at}, dataset_version: {dataset_version},"
                  f" source: internetinmyanmar.com/data\n")
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    for row in rows:
        cleaned = {
            k: (json.dumps(v) if isinstance(v, (list, dict)) else v)
            for k, v in row.items()
        }
        writer.writerow(cleaned)
    return buf.getvalue()


def strip_internal(events: list[dict]) -> list[dict]:
    """Remove internal _ fields before serialization."""
    return [{k: v for k, v in e.items() if not k.startswith("_")} for e in events]


# ---------------------------------------------------------------------------
# GitHub push
# ---------------------------------------------------------------------------

def push_to_github(file_contents: dict[str, str], version: str) -> None:
    """
    Commit all output files in a single batch via GitHub API.
    file_contents: {repo_path: content_string}
    """
    if not GITHUB_TOKEN:
        log.warning("No GITHUB_TOKEN — skipping GitHub push")
        return

    from github import Github, GithubException

    g    = Github(GITHUB_TOKEN)
    repo = g.get_repo(REPO_NAME)
    ts   = now_utc().strftime("%Y-%m-%d %H:%M")

    import base64
    pushed = 0
    for repo_path, content in file_contents.items():
        is_json = repo_path.endswith(".json")
        try:
            try:
                existing = repo.get_contents(repo_path, ref="main")
                existing_content = base64.b64decode(existing.content).decode("utf-8")
                if not _content_changed(existing_content, content, is_json):
                    log.debug("No data change in %s — skipping", repo_path)
                    continue
                repo.update_file(
                    repo_path,
                    f"data: update {repo_path.split('/')[-1]} {ts} UTC",
                    content,
                    existing.sha,
                    branch="main",
                )
            except GithubException:
                repo.create_file(
                    repo_path,
                    f"data: create {repo_path.split('/')[-1]} {ts} UTC",
                    content,
                    branch="main",
                )
            log.info("Pushed: %s", repo_path)
            pushed += 1
        except Exception as exc:
            log.error("Failed to push %s: %s", repo_path, exc)

    log.info("GitHub push complete: %d/%d files updated", pushed, len(file_contents))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry_run: bool = False) -> None:
    log.info("=== process_datasets starting (dry_run=%s) ===", dry_run)
    warnings: list[str] = []

    # ── Load ──────────────────────────────────────────────────────────────
    sources, load_warnings = load_raw_sources()
    warnings.extend(load_warnings)

    version = dataset_version(sources)   # input-fingerprint, not wall-clock

    # ── Alert on stale sources (full run only) ────────────────────────────
    if warnings and not dry_run:
        stale = [w for w in warnings if "stale" in w or "Missing" in w]
        if stale:
            msg = "⚠️ *process\\_datasets* — stale/missing sources:\n" + "\n".join(
                f"• `{w}`" for w in stale
            )
            _telegram_alert(msg)

    # ── Normalize ─────────────────────────────────────────────────────────
    bgp_norm  = normalize_bgp_outages(sources.get("bgp_outages") or [])
    kit_norm  = normalize_keepiton(sources.get("keepiton") or {})
    cf_norm   = normalize_cf_radar(sources.get("cf_radar") or {})

    # ── Join ──────────────────────────────────────────────────────────────
    unified   = join_unified_events(bgp_norm, kit_norm, cf_norm)

    # ── Derive metrics ────────────────────────────────────────────────────
    metrics   = compute_metrics(
        unified,
        sources.get("ooni_daily") or [],
        sources.get("blocked_sites") or {},
        sources.get("cf_traffic") or {},
        sources.get("bgp_status") or {},
    )
    log.info("Metrics: %s", json.dumps(metrics, default=str))

    # ── Prepare processing log ────────────────────────────────────────────
    proc_log = {
        "bgp_outages_loaded":    len(bgp_norm),
        "keepiton_events_loaded": len(kit_norm),
        "unified_events_total":  len(unified),
        "bgp_events_in_unified": sum(1 for e in unified if "bgp" in e["sources"]),
        "kit_events_in_unified": sum(1 for e in unified if "keepiton" in e["sources"]),
        "warnings":              warnings,
    }

    header = make_header(version, proc_log)

    # ── Export: unified_events.json ───────────────────────────────────────
    unified_clean = strip_internal(unified)
    unified_json  = export_json(Path(), header, unified_clean)

    # ── Export: metrics_snapshot.json ─────────────────────────────────────
    metrics_json  = export_metrics_json(Path(), header, metrics)

    csv_meta = dict(generated_at=header["generated_at"], dataset_version=version)

    # ── Export: bgp_events.csv ────────────────────────────────────────────
    bgp_csv_fields = [
        "asn", "isp_name", "started_at", "ended_at",
        "duration_minutes", "min_visibility_pct", "resolved",
    ]
    bgp_clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in bgp_norm]
    bgp_csv   = records_to_csv(bgp_clean, bgp_csv_fields, **csv_meta)

    # ── Export: keepiton_shutdowns.csv ────────────────────────────────────
    kit_csv_fields = [
        "id", "start_date", "end_date", "ongoing", "duration_days",
        "type", "scope", "perpetrator", "services", "source_url",
    ]
    kit_clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in kit_norm]
    kit_csv   = records_to_csv(kit_clean, kit_csv_fields, **csv_meta)

    # ── Export: ooni_timeseries.csv ───────────────────────────────────────
    ooni_monthly = sources.get("ooni_daily") or []   # use daily for recency
    ooni_all     = sorted(
        (sources.get("ooni_monthly") or []) + ooni_monthly,
        key=lambda r: r.get("period", ""),
    )
    # Deduplicate by period
    seen_periods: set[str] = set()
    ooni_deduped: list[dict] = []
    for r in reversed(ooni_all):
        p = r.get("period", "")
        if p not in seen_periods:
            seen_periods.add(p)
            ooni_deduped.insert(0, r)

    ooni_csv_fields = [
        "period", "measurement_start_day", "measurement_count",
        "anomaly_count", "anomaly_rate", "confirmed_count",
        "failure_count", "ok_count",
    ]
    ooni_csv = records_to_csv(ooni_deduped, ooni_csv_fields, **csv_meta)

    # ── Export: blocked_sites.csv ─────────────────────────────────────────
    blocked = sources.get("blocked_sites") or {}
    sites   = blocked.get("sites", [])
    sites_csv_fields = ["domain", "category", "anomaly_count", "total", "rate"]
    sites_csv = records_to_csv(sites, sites_csv_fields, **csv_meta)

    if dry_run:
        print("\n=== DRY RUN — outputs not pushed ===")
        print(f"dataset_version: {version}")
        print(f"unified_events:  {len(unified)} events")
        print(f"bgp_events.csv:  {len(bgp_clean)} rows")
        print(f"keepiton.csv:    {len(kit_clean)} rows")
        print(f"ooni_ts.csv:     {len(ooni_deduped)} rows")
        print(f"blocked_sites.csv: {len(sites)} rows")
        print(f"warnings:        {warnings}")
        print("\nMetrics snapshot:")
        print(json.dumps(metrics, indent=2, default=str))
        return

    # ── Write local files (for Astro to pick up in the same repo) ─────────
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "unified_events.json").write_text(unified_json, encoding="utf-8")
    (OUTPUT_DIR / "metrics_snapshot.json").write_text(metrics_json, encoding="utf-8")
    (OUTPUT_DIR / "bgp_events.csv").write_text(bgp_csv, encoding="utf-8")
    (OUTPUT_DIR / "keepiton_shutdowns.csv").write_text(kit_csv, encoding="utf-8")
    (OUTPUT_DIR / "ooni_timeseries.csv").write_text(ooni_csv, encoding="utf-8")
    (OUTPUT_DIR / "blocked_sites.csv").write_text(sites_csv, encoding="utf-8")
    log.info("Wrote output files to %s", OUTPUT_DIR)

    # ── Push to GitHub ─────────────────────────────────────────────────────
    push_to_github(
        {
            OUTPUT_FILES["unified_events"]:    unified_json,
            OUTPUT_FILES["metrics_snapshot"]:  metrics_json,
            OUTPUT_FILES["bgp_events"]:        bgp_csv,
            OUTPUT_FILES["keepiton_shutdowns"]: kit_csv,
            OUTPUT_FILES["ooni_timeseries"]:   ooni_csv,
            OUTPUT_FILES["blocked_sites"]:     sites_csv,
        },
        version,
    )

    log.info("=== process_datasets done (version=%s) ===", version)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Unified data layer orchestrator")
    parser.add_argument("--dry-run", action="store_true",
                        help="Process data but do not write files or push to GitHub")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
