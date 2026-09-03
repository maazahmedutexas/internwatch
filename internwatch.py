#!/usr/bin/env python3
"""
internwatch.py

Polls company ATS endpoints (the actual source of truth, not LinkedIn) and the
community-maintained SimplifyJobs listing feed, filters for high-tier internship
roles that match your profile, and pushes a notification the moment a new one
appears.

Usage:
    python internwatch.py --check-sources     # health check every source, fix broken slugs
    python internwatch.py --seed              # mark everything currently open as "seen" (RUN THIS FIRST)
    python internwatch.py --once --dry-run    # one pass, print what it would send
    python internwatch.py --once              # one pass, actually notify (use with cron / GH Actions)
    python internwatch.py                     # run forever, polling on an interval
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import requests
import yaml

HERE = Path(__file__).resolve().parent
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HOURS_PER_MONTH = 173.33
HOURS_PER_YEAR = 2080.0

log = logging.getLogger("internwatch")


# ----------------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------------

@dataclass
class Job:
    company: str
    title: str
    url: str
    location: str = ""
    source: str = ""
    description: str = ""
    posted: str = ""
    pay_low: float | None = None      # normalized to USD/hour
    pay_high: float | None = None
    pay_is_estimate: bool = False
    score: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Stable identity across sources. Title + company + coarse location."""
        t = re.sub(r"[^a-z0-9]+", " ", self.title.lower()).strip()
        t = re.sub(r"\b(20\d\d|summer|fall|spring|winter|intern|internship|co op|coop)\b", "", t)
        t = re.sub(r"\s+", " ", t).strip()
        loc = re.sub(r"[^a-z]+", "", self.location.lower())[:12]
        raw = f"{self.company.lower()}|{t}|{loc}"
        return hashlib.sha256(raw.encode()).hexdigest()[:20]


# ----------------------------------------------------------------------------
# HTTP helper
# ----------------------------------------------------------------------------

def http(method: str, url: str, **kw) -> requests.Response | None:
    headers = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
    headers.update(kw.pop("headers", {}))
    try:
        r = requests.request(method, url, headers=headers, timeout=kw.pop("timeout", 25), **kw)
        if r.status_code >= 400:
            log.debug("%s %s -> HTTP %s", method, url, r.status_code)
            return None
        return r
    except requests.RequestException as e:
        log.debug("%s %s -> %s", method, url, e)
        return None


def jget(r: requests.Response | None) -> Any:
    if r is None:
        return None
    try:
        return r.json()
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# Sources
# Each returns a list[Job]. Every one is wrapped so a single broken endpoint
# never kills the run.
# ----------------------------------------------------------------------------

def src_simplify(cfg: dict) -> list[Job]:
    """
    Community feed. This is usually the FASTEST signal in existence for new grad
    and intern postings, often within minutes, because thousands of people watch it.
    Repo name rolls over each cycle, so config lists candidates.
    """
    jobs: list[Job] = []
    for url in cfg["sources"]["simplify_urls"]:
        data = jget(http("GET", url))
        if not isinstance(data, list):
            continue
        for row in data:
            if row.get("active") is False:
                continue
            locs = row.get("locations") or []
            loc = ", ".join(locs) if isinstance(locs, list) else str(locs)
            link = row.get("url") or ""
            if not link:
                continue
            jobs.append(Job(
                company=(row.get("company_name") or "").strip(),
                title=(row.get("title") or "").strip(),
                url=link,
                location=loc,
                source="simplify",
                posted=_epoch_str(row.get("date_posted")),
            ))
        if jobs:
            break  # first repo that answers wins
    return jobs


def _epoch_str(v: Any) -> str:
    try:
        return datetime.fromtimestamp(float(v), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def src_workday(company: str, spec: dict, query: str) -> list[Job]:
    """
    Workday's CXS API. NVIDIA, AMD, Micron, Qualcomm, Intel, Marvell, ADI, AMAT,
    Salesforce and a hundred others all run on this. One implementation, many companies.
    """
    host, tenant, site = spec["host"], spec["tenant"], spec["site"]
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    out: list[Job] = []
    for offset in (0, 20, 40):
        data = jget(http(
            "POST", url,
            headers={"Content-Type": "application/json"},
            json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": query},
        ))
        postings = (data or {}).get("jobPostings") or []
        if not postings:
            break
        for p in postings:
            path = p.get("externalPath") or ""
            out.append(Job(
                company=company,
                title=(p.get("title") or "").strip(),
                url=f"https://{host}/{site}{path}",
                location=(p.get("locationsText") or "").strip(),
                source="workday",
                posted=(p.get("postedOn") or "").replace("Posted ", ""),
            ))
        if len(postings) < 20:
            break
    return out


def src_greenhouse(company: str, token: str) -> list[Job]:
    data = jget(http("GET", f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"))
    out = []
    for j in (data or {}).get("jobs", []):
        out.append(Job(
            company=company,
            title=(j.get("title") or "").strip(),
            url=j.get("absolute_url") or "",
            location=((j.get("location") or {}).get("name") or "").strip(),
            source="greenhouse",
            description=(j.get("content") or "")[:6000],
            posted=(j.get("updated_at") or "")[:10],
        ))
    return out


def src_lever(company: str, token: str) -> list[Job]:
    data = jget(http("GET", f"https://api.lever.co/v0/postings/{token}?mode=json"))
    out = []
    for j in (data or []):
        cat = j.get("categories") or {}
        out.append(Job(
            company=company,
            title=(j.get("text") or "").strip(),
            url=j.get("hostedUrl") or "",
            location=(cat.get("location") or "").strip(),
            source="lever",
            description=(j.get("descriptionPlain") or "")[:6000],
        ))
    return out


def src_microsoft(query: str) -> list[Job]:
    out = []
    for page in (1, 2):
        data = jget(http(
            "GET",
            "https://gcsservices.careers.microsoft.com/search/api/v1/search",
            params={"q": query, "l": "en_us", "pg": page, "pgSz": 20,
                    "o": "Recent", "flt": "true"},
        ))
        res = ((data or {}).get("operationResult") or {}).get("result") or {}
        jobs = res.get("jobs") or []
        if not jobs:
            break
        for j in jobs:
            jid = j.get("jobId") or ""
            props = j.get("properties") or {}
            locs = props.get("locations") or []
            out.append(Job(
                company="Microsoft",
                title=(j.get("title") or "").strip(),
                url=f"https://jobs.careers.microsoft.com/global/en/job/{jid}",
                location=", ".join(locs[:3]) if isinstance(locs, list) else str(locs),
                source="microsoft",
                posted=(j.get("postingDate") or "")[:10],
                description=(props.get("description") or "")[:6000],
            ))
    return out


def src_amazon(query: str) -> list[Job]:
    data = jget(http(
        "GET", "https://www.amazon.jobs/en/search.json",
        params={"base_query": query, "result_limit": 100, "sort": "recent",
                "country": "USA", "offset": 0},
        headers={"Accept": "application/json"},
    ))
    out = []
    for j in (data or {}).get("jobs", []):
        out.append(Job(
            company="Amazon",
            title=(j.get("title") or "").strip(),
            url="https://www.amazon.jobs" + (j.get("job_path") or ""),
            location=(j.get("location") or j.get("normalized_location") or "").strip(),
            source="amazon",
            posted=(j.get("posted_date") or ""),
            description=(j.get("description") or "")[:6000],
        ))
    return out


def src_apple(query: str) -> list[Job]:
    """Apple rotates its search endpoint. Try the current one, fall back to the legacy."""
    out: list[Job] = []
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://jobs.apple.com/en-us/search"})
    try:
        s.get("https://jobs.apple.com/en-us/search", timeout=20)
    except requests.RequestException:
        pass
    body = {"query": query, "filters": {"postingpostLocation": ["postLocation-USA"]},
            "page": 1, "locale": "en-us", "sort": "newest"}
    for url in ("https://jobs.apple.com/api/role/search",
                "https://jobs.apple.com/api/v1/search"):
        try:
            r = s.post(url, json=body, timeout=25)
            data = r.json() if r.ok else None
        except (requests.RequestException, ValueError):
            data = None
        if not isinstance(data, dict):
            continue
        rows = data.get("searchResults") or data.get("res") or []
        if not isinstance(rows, list) or not rows:
            continue
        for j in rows:
            if not isinstance(j, dict):
                continue  # some Apple responses mix in plain strings here
            pid = j.get("positionId") or j.get("id") or ""
            locs = j.get("locations") or []
            loc = ", ".join(l.get("name", "") for l in locs if isinstance(l, dict)) if locs else \
                  (j.get("postingTitle") and "" or "")
            out.append(Job(
                company="Apple",
                title=(j.get("postingTitle") or j.get("jobTitle") or "").strip(),
                url=f"https://jobs.apple.com/en-us/details/{pid}",
                location=loc or (j.get("locationName") or ""),
                source="apple",
                posted=(j.get("postDateInGMT") or "")[:10],
                description=(j.get("jobSummary") or "")[:6000],
            ))
        break
    return out


def src_tesla(_: str) -> list[Job]:
    data = jget(http("GET", "https://www.tesla.com/cua-api/apps/careers/state"))
    listings = (data or {}).get("listings") or []
    lookup = (data or {}).get("lookup") or {}
    locs = lookup.get("locations") or {}
    out = []
    for j in listings:
        jid = j.get("id")
        loc_id = str(j.get("l") or "")
        out.append(Job(
            company="Tesla",
            title=(j.get("t") or "").strip(),
            url=f"https://www.tesla.com/careers/search/job/{jid}",
            location=str(locs.get(loc_id, "")),
            source="tesla",
        ))
    return out


def src_google(query: str) -> list[Job]:
    out = []
    for page in (1, 2):
        data = jget(http(
            "GET",
            "https://www.google.com/about/careers/applications/api/v3/search/",
            params={"q": query, "page": page, "page_size": 50,
                    "employment_type": "INTERN", "location": "United States",
                    "sort_by": "date"},
        ))
        jobs = (data or {}).get("jobs") or []
        if not jobs:
            break
        for j in jobs:
            slug = (j.get("id") or "").split("/")[-1]
            locs = j.get("locations") or []
            loc = ", ".join(l.get("display", "") for l in locs if isinstance(l, dict))
            out.append(Job(
                company="Google",
                title=(j.get("title") or "").strip(),
                url=f"https://www.google.com/about/careers/applications/jobs/results/{slug}",
                location=loc,
                source="google",
                posted=(j.get("publish_date") or "")[:10],
                description=" ".join(j.get("description", "").split())[:6000],
            ))
    return out


def src_meta(query: str) -> list[Job]:
    """
    Meta's careers site is a GraphQL SPA with a rotating doc_id, so a direct API
    call is brittle by nature. The Simplify feed reliably covers Meta, so treat
    this as a bonus rather than a dependency.
    """
    data = jget(http(
        "POST", "https://www.metacareers.com/graphql",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"doc_id": "9114524511922157",
              "variables": json.dumps({"search_input": {
                  "q": query, "divisions": [], "offices": [],
                  "results_per_page": 100, "sort_by_new": True}})},
    ))
    rows = (((data or {}).get("data") or {}).get("job_search_with_featured_jobs") or {}).get("all_jobs") or []
    out = []
    for j in rows:
        out.append(Job(
            company="Meta",
            title=(j.get("title") or "").strip(),
            url=f"https://www.metacareers.com/jobs/{j.get('id')}/",
            location=", ".join(j.get("locations") or []),
            source="meta",
        ))
    return out


# ----------------------------------------------------------------------------
# Source registry
# ----------------------------------------------------------------------------

def build_sources(cfg: dict) -> list[tuple[str, Callable[[], list[Job]]]]:
    q = cfg["sources"]["query"]
    reg: list[tuple[str, Callable[[], list[Job]]]] = [
        ("simplify-feed", lambda: src_simplify(cfg)),
        ("microsoft", lambda: src_microsoft(q)),
        ("amazon", lambda: src_amazon(q)),
        ("apple", lambda: src_apple(q)),
        ("google", lambda: src_google(q)),
        ("meta", lambda: src_meta(q)),
        ("tesla", lambda: src_tesla(q)),
    ]
    for name, spec in (cfg["sources"].get("workday") or {}).items():
        reg.append((f"workday:{name}", lambda n=name, s=spec: src_workday(n, s, q)))
    for name, token in (cfg["sources"].get("greenhouse") or {}).items():
        reg.append((f"greenhouse:{name}", lambda n=name, t=token: src_greenhouse(n, t)))
    for name, token in (cfg["sources"].get("lever") or {}).items():
        reg.append((f"lever:{name}", lambda n=name, t=token: src_lever(n, t)))
    return reg


def collect(cfg: dict) -> list[Job]:
    jobs: list[Job] = []
    for name, fn in build_sources(cfg):
        t0 = time.time()
        try:
            got = fn() or []
        except Exception as e:  # noqa: BLE001 - one bad source must not kill the run
            log.warning("source %-24s FAILED: %s", name, e)
            continue
        log.info("source %-24s %4d jobs  (%.1fs)", name, len(got), time.time() - t0)
        jobs.extend(got)
    return jobs


# ----------------------------------------------------------------------------
# Pay parsing
# ----------------------------------------------------------------------------

_NUM = r"\$\s*([\d][\d,]*(?:\.\d+)?)"
_SEP = r"\s*(?:-|to|through|\u2013|\u2014)\s*"
_PAY_PATTERNS = [
    (re.compile(_NUM + "(?:" + _SEP + r"\$?\s*([\d][\d,]*(?:\.\d+)?))?"
                r"\s*(?:USD\s*)?(?:/|\s*per\s+|\s+an?\s+)\s*(?:hr|hour)", re.I), 1.0),
    (re.compile(_NUM + "(?:" + _SEP + r"\$?\s*([\d][\d,]*(?:\.\d+)?))?"
                r"\s*(?:USD\s*)?(?:/|\s*per\s+|\s+a\s+)\s*(?:mo|month)", re.I), 1 / HOURS_PER_MONTH),
    (re.compile(_NUM + "(?:" + _SEP + r"\$?\s*([\d][\d,]*(?:\.\d+)?))?"
                r"\s*(?:USD\s*)?(?:/|\s*per\s+|\s+a\s+)\s*(?:yr|year|annum)|annually", re.I), 1 / HOURS_PER_YEAR),
]


def parse_pay(text: str) -> tuple[float | None, float | None]:
    """Pull a pay range out of free text and normalize to USD/hour."""
    if not text:
        return None, None
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = clean.replace("&nbsp;", " ").replace("&amp;", "&")
    for pat, mult in _PAY_PATTERNS:
        m = pat.search(clean)
        if not m:
            continue
        try:
            lo = float(m.group(1).replace(",", "")) * mult
        except (TypeError, ValueError, AttributeError):
            continue
        hi = lo
        if m.lastindex and m.lastindex >= 2 and m.group(2):
            try:
                hi = float(m.group(2).replace(",", "")) * mult
            except ValueError:
                hi = lo
        if 8 <= lo <= 400:  # sanity bound, kills false positives like "$500 stipend"
            return round(lo, 2), round(hi, 2)
    return None, None


def apply_pay(job: Job, cfg: dict) -> None:
    lo, hi = parse_pay(f"{job.title}\n{job.description}")
    if lo is not None:
        job.pay_low, job.pay_high, job.pay_is_estimate = lo, hi, False
        return
    est = (cfg.get("known_pay_hourly") or {}).get(job.company)
    if est:
        job.pay_low = job.pay_high = float(est)
        job.pay_is_estimate = True


# ----------------------------------------------------------------------------
# Filtering + scoring
# ----------------------------------------------------------------------------

def norm_company(name: str, aliases: dict[str, str]) -> str:
    n = re.sub(r"[^a-z0-9 ]", "", (name or "").lower()).strip()
    n = re.sub(r"\b(inc|corp|corporation|llc|ltd|technologies|technology|labs|the)\b", "", n).strip()
    n = re.sub(r"\s+", " ", n)
    return aliases.get(n, n)


def any_in(text: str, words: Iterable[str]) -> str | None:
    for w in words:
        if w.lower() in text:
            return w
    return None


def location_ok(job: Job, cfg: dict) -> tuple[bool, int]:
    loc = job.location.lower()
    lf = cfg["locations"]
    if not loc:
        return (lf["allow_unknown"], 0)
    if any_in(loc, lf["block"]):
        return (False, 0)
    if any_in(loc, lf["remote_ok_terms"]):
        return (True, 1)
    tier1 = any_in(loc, lf["tier1"])
    if tier1:
        return (True, 2)
    tier2 = any_in(loc, lf["tier2"])
    if tier2:
        return (True, 1)
    # bare US state code or "United States"
    if re.search(r"\b(usa|united states|u\.s\.)\b", loc):
        return (lf["allow_unknown"], 0)
    return (False, 0)


def evaluate(job: Job, cfg: dict) -> bool:
    """Mutates job.score / job.reasons. Returns True if it should be notified."""
    aliases = cfg["companies"]["aliases"]
    tier1 = {norm_company(c, aliases) for c in cfg["companies"]["tier1"]}
    tier2 = {norm_company(c, aliases) for c in cfg["companies"]["tier2"]}
    cn = norm_company(job.company, aliases)

    if cn in tier1:
        job.score += 3
    elif cn in tier2:
        job.score += 2
        job.reasons.append("tier2")
    else:
        return False

    title = job.title.lower()
    blob = f"{title} {job.description.lower()[:2000]}"
    rk = cfg["roles"]

    if not any_in(title, rk["must_any"]):
        return False
    if any_in(title, rk["block"]):
        return False
    if any_in(title, rk["block_terms"]):
        return False

    # ---- undergrad only -----------------------------------------------------
    ug = rk["undergrad"]
    if any_in(title, ug["title_block"]):
        return False
    body = job.description.lower()
    if body and any_in(body, ug["body_block"]) and not any_in(body, ug["undergrad_signals"]):
        return False

    hit_embedded = any_in(blob, rk["embedded"])
    hit_swe = any_in(blob, rk["swe"])
    hit_hw = any_in(blob, rk["hardware"])
    if hit_embedded and hit_swe:
        job.score += 5
        job.reasons.append("embedded-swe")
    elif hit_embedded:
        job.score += 4
        job.reasons.append("embedded")
    elif hit_swe:
        job.score += 3
        job.reasons.append("swe")
    elif hit_hw:
        job.score += 1
        job.reasons.append("hardware")
    else:
        return False

    ok, loc_pts = location_ok(job, cfg)
    if not ok:
        return False
    job.score += loc_pts

    # ---- pay ---------------------------------------------------------------
    apply_pay(job, cfg)
    pc = cfg["pay"]
    floor = pc["floor_hourly"]
    great = pc.get("great_hourly") or floor * 1.25
    if job.pay_low is None:
        if not pc["notify_if_unknown"]:
            return False
        job.reasons.append("pay?")
    else:
        top = job.pay_high or job.pay_low
        if top >= great:
            job.score += 3
            job.reasons.append("TOP PAY")
        elif top >= floor:
            job.score += 1
        else:
            if not pc["notify_if_below_floor"]:
                return False
            job.score -= 3
            job.reasons.append("LOW PAY")

    if any_in(title, cfg["roles"]["term_boost"]):
        job.score += 2
        job.reasons.append("term-match")

    return job.score >= cfg["min_score"]


# ----------------------------------------------------------------------------
# State
# ----------------------------------------------------------------------------

class Store:
    """
    Plain JSON, one line per key, sorted. Chosen over sqlite so the state file
    diffs and compresses well in git, which is what the GitHub Actions runner
    needs to persist state between runs.
    """

    def __init__(self, path: Path, retention_days: int = 400):
        self.path = path
        self.retention_days = retention_days
        self.seen: dict[str, str] = {}
        if path.exists():
            try:
                blob = json.loads(path.read_text() or "{}")
                self.seen = dict(blob.get("seen") or {})
            except (ValueError, OSError) as e:
                log.warning("state file unreadable (%s), starting fresh", e)
        self._dirty = False

    def is_new(self, job: Job) -> bool:
        return job.key not in self.seen

    def mark(self, job: Job, notified: bool = True) -> None:
        if job.key not in self.seen:
            self.seen[job.key] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self._dirty = True

    def count(self) -> int:
        return len(self.seen)

    def _prune(self) -> int:
        cutoff = time.time() - self.retention_days * 86400
        drop = []
        for k, d in self.seen.items():
            try:
                ts = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
            if ts < cutoff:
                drop.append(k)
        for k in drop:
            del self.seen[k]
        return len(drop)

    def save(self) -> None:
        n = self._prune()
        if n:
            log.info("pruned %d state entries older than %dd", n, self.retention_days)
            self._dirty = True
        if not self._dirty:
            return
        payload = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "seen": dict(sorted(self.seen.items()))}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=0, sort_keys=True))
        tmp.replace(self.path)
        self._dirty = False


# ----------------------------------------------------------------------------
# Notifiers
# ----------------------------------------------------------------------------

def fmt_pay(job: Job) -> str:
    if job.pay_low is None:
        return "pay n/a"
    tag = "~" if job.pay_is_estimate else ""
    if job.pay_high and job.pay_high != job.pay_low:
        return f"{tag}${job.pay_low:.0f}-{job.pay_high:.0f}/hr"
    return f"{tag}${job.pay_low:.0f}/hr"


def fmt_sms(job: Job) -> str:
    loc = job.location[:34] or "loc n/a"
    flags = " ".join(f"[{r}]" for r in job.reasons
                     if r in ("LOW PAY", "TOP PAY", "tier2", "pay?"))
    return (f"[{job.score}] {job.company}: {job.title[:60]}\n"
            f"{loc} | {fmt_pay(job)} {flags}\n{job.url}")


def notify_twilio(cfg: dict, body: str) -> bool:
    c = cfg["notify"]["twilio"]
    sid = os.environ.get("TWILIO_SID") or c.get("account_sid")
    tok = os.environ.get("TWILIO_TOKEN") or c.get("auth_token")
    if not sid or not tok:
        log.error("twilio: missing TWILIO_SID / TWILIO_TOKEN")
        return False
    r = http("POST", f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
             auth=(sid, tok), data={"From": c["from_number"], "To": c["to_number"], "Body": body})
    return r is not None


def notify_ntfy(cfg: dict, body: str) -> bool:
    c = cfg["notify"]["ntfy"]
    # env wins, so a public repo can keep the topic (which is the only secret) out of git
    server = os.environ.get("NTFY_SERVER") or c["server"]
    topic = os.environ.get("NTFY_TOPIC") or c.get("topic") or ""
    if not topic or "CHANGE" in topic:
        log.error("ntfy: no topic set (NTFY_TOPIC env or notify.ntfy.topic in config)")
        return False
    c = {"server": server, "topic": topic}
    r = http("POST", f"{c['server'].rstrip('/')}/{c['topic']}",
             data=body.encode("utf-8"),
             headers={"Title": "New internship posted", "Priority": "high", "Tags": "rocket"})
    return r is not None


def notify_discord(cfg: dict, body: str) -> bool:
    url = os.environ.get("DISCORD_WEBHOOK") or cfg["notify"]["discord"].get("webhook_url")
    if not url:
        return False
    return http("POST", url, json={"content": body[:1900]}) is not None


def notify_telegram(cfg: dict, body: str) -> bool:
    c = cfg["notify"]["telegram"]
    tok = os.environ.get("TELEGRAM_TOKEN") or c.get("bot_token")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or c.get("chat_id")
    if not tok or not chat:
        return False
    return http("POST", f"https://api.telegram.org/bot{tok}/sendMessage",
                json={"chat_id": chat, "text": body,
                      "disable_web_page_preview": True}) is not None


NOTIFIERS = {"twilio": notify_twilio, "ntfy": notify_ntfy,
             "discord": notify_discord, "telegram": notify_telegram}


def send(cfg: dict, body: str, dry: bool) -> None:
    if dry:
        print("--- WOULD SEND ---\n" + body + "\n")
        return
    for name in cfg["notify"]["channels"]:
        fn = NOTIFIERS.get(name)
        if not fn:
            log.warning("unknown channel %r", name)
            continue
        try:
            if fn(cfg, body):
                log.info("notified via %s", name)
            else:
                log.error("channel %s failed", name)
        except Exception as e:  # noqa: BLE001
            log.error("channel %s raised: %s", name, e)


# ----------------------------------------------------------------------------
# Passes
# ----------------------------------------------------------------------------

def one_pass(cfg: dict, store: Store, dry: bool, seed: bool) -> int:
    raw = collect(cfg)
    log.info("collected %d raw postings", len(raw))

    seen_keys: set[str] = set()
    hits: list[Job] = []
    for job in raw:
        if not job.title or not job.url:
            continue
        if job.key in seen_keys:
            continue
        seen_keys.add(job.key)
        if not evaluate(job, cfg):
            continue
        hits.append(job)

    hits.sort(key=lambda j: -j.score)
    log.info("%d matched your filters", len(hits))

    new = [j for j in hits if store.is_new(j)]
    log.info("%d are new since last run", len(new))

    if seed:
        for j in hits:
            store.mark(j, notified=True)
        # also suppress non-matching current postings so filter tweaks stay quiet
        for job in raw:
            if job.title and job.url:
                store.mark(job, notified=True)
        store.save()
        print(f"Seeded {store.count()} postings as already-seen. "
              f"Future runs will only alert on genuinely new roles.")
        return 0

    batch = cfg["notify"].get("batch_size", 1)
    if batch <= 1:
        for j in new:
            send(cfg, fmt_sms(j), dry)
            if not dry:
                time.sleep(1)
    else:
        for i in range(0, len(new), batch):
            chunk = new[i:i + batch]
            send(cfg, "\n\n".join(fmt_sms(j) for j in chunk), dry)

    if not dry:
        for j in new:
            store.mark(j, notified=True)
        store.save()
    return len(new)


def check_sources(cfg: dict) -> None:
    print(f"{'SOURCE':<26} {'STATUS':<8} {'COUNT':>6}  SAMPLE")
    print("-" * 100)
    for name, fn in build_sources(cfg):
        try:
            got = fn() or []
        except Exception as e:  # noqa: BLE001
            print(f"{name:<26} {'ERROR':<8} {'-':>6}  {type(e).__name__}: {e}")
            continue
        status = "OK" if got else "EMPTY"
        sample = f"{got[0].title[:45]} @ {got[0].location[:22]}" if got else \
                 "check host/tenant/site slug in config.yaml"
        print(f"{name:<26} {status:<8} {len(got):>6}  {sample}")


# ----------------------------------------------------------------------------
# Entry
# ----------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Watch top-tier internship postings.")
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    ap.add_argument("--state", default=str(HERE / "seen.json"),
                    help="JSON file tracking already-notified postings")
    ap.add_argument("--once", action="store_true", help="single pass then exit (for cron)")
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending")
    ap.add_argument("--seed", action="store_true", help="mark all current postings as seen")
    ap.add_argument("--check-sources", action="store_true", help="health check every endpoint")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    cfg = yaml.safe_load(Path(a.config).read_text())

    if a.check_sources:
        check_sources(cfg)
        return 0

    store = Store(Path(a.state), cfg.get("state_retention_days", 400))

    if a.seed:
        one_pass(cfg, store, dry=False, seed=True)
        return 0

    if a.once:
        one_pass(cfg, store, dry=a.dry_run, seed=False)
        return 0

    interval = cfg.get("poll_seconds", 600)
    log.info("watching every %ss. ctrl-c to stop.", interval)
    while True:
        try:
            n = one_pass(cfg, store, dry=a.dry_run, seed=False)
            if n:
                log.info("sent %d alerts", n)
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # noqa: BLE001
            log.error("pass failed: %s", e)
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
