"""نظام متابعة تعليقات قوقل — رشف وارميما (ملف واحد).

python monitor.py --mode collect|daily|weekly|analyze
"""

# ======================================================================
# common
# ======================================================================
"""Shared helpers: config, data store, time, Telegram."""
import html
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
REVIEWS_PATH = DATA_DIR / "reviews.json"
STATE_PATH = DATA_DIR / "state.json"


def log(*args):
    print("[monitor]", *args, file=sys.stderr, flush=True)


# ---------- config ----------
def load_config():
    with open(ROOT / "config.json", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["_by_place"] = {b["place_id"]: b for b in cfg["branches"]}
    cfg["_by_id"] = {b["id"]: b for b in cfg["branches"]}
    return cfg


def env(name, required=False, default=None):
    val = os.environ.get(name, "").strip()
    if not val:
        if required:
            raise SystemExit(f"Missing required secret/env: {name}")
        return default
    return val


# ---------- time ----------
def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def local_str(dt, cfg, fmt="%Y-%m-%d %H:%M"):
    off = timedelta(hours=cfg.get("timezone_offset_hours", 3))
    return (dt.astimezone(timezone.utc) + off).strftime(fmt)


# ---------- store ----------
def _read_json(path, default):
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def _write_json(path, obj):
    DATA_DIR.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    tmp.replace(path)


def load_store():
    return _read_json(REVIEWS_PATH, {"updated_at": None, "reviews": []})


def save_store(store):
    store["updated_at"] = iso(now_utc())
    store["reviews"].sort(key=lambda r: r.get("published_at") or "", reverse=True)
    _write_json(REVIEWS_PATH, store)


def load_state():
    return _read_json(STATE_PATH, {})


def save_state(state):
    _write_json(STATE_PATH, state)


# ---------- telegram ----------
def esc(s):
    return html.escape(str(s or ""), quote=False)


def _chunks(text, limit=3900):
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur)
    return parts


def telegram_send(chat_ids, text):
    """Send an HTML message to each chat id. DRY_RUN=1 prints instead."""
    chat_ids = [str(c) for c in chat_ids if str(c).strip()]
    if not chat_ids:
        return
    if os.environ.get("DRY_RUN") == "1" or not env("TELEGRAM_BOT_TOKEN"):
        for c in chat_ids:
            print(f"\n----- Telegram → {c} -----\n{text}")
        return
    token = env("TELEGRAM_BOT_TOKEN")
    for chat in chat_ids:
        for part in _chunks(text):
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat,
                    "text": part,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )
            if not r.ok:
                log(f"Telegram error for {chat}: {r.status_code} {r.text[:200]}")


def owner_chats():
    raw = env("TELEGRAM_OWNER_CHAT_ID", default="")
    return [c.strip() for c in raw.split(",") if c.strip()]

# ======================================================================
# sources
# ======================================================================
"""Review sources. Each returns a list of normalized review dicts.

- ApifySource: works today (Google Maps scraper on Apify).
- GBPSource:   official Google Business Profile API. Switch config "source"
               to "gbp" once Google approves your API access.
"""
import re

import requests


STAR_WORDS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}


def _clean(s):
    """Google review text sometimes contains literal <br> tags."""
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I).replace("&nbsp;", " ")
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def _normalize(branch, *, rid, stars, text, published, url=None,
               reply=None, translated=None, source):
    text, translated, reply = _clean(text), _clean(translated), _clean(reply)
    return {
        "id": str(rid),
        "source": source,
        "branch_id": branch["id"],
        "brand": branch["brand"],
        "branch_name": branch["name"],
        "city": branch.get("city"),
        "stars": int(stars) if stars else None,
        "text": (text or "").strip(),
        "text_translated": (translated or "").strip() or None,
        "published_at": published,
        "url": url,
        "owner_reply": (reply or "").strip() or None,
    }


class ApifySource:
    name = "apify"

    def __init__(self, cfg):
        self.cfg = cfg
        self.token = env("APIFY_TOKEN", required=True)

    def fetch(self, since):
        a = self.cfg["apify"]
        body = {
            "placeIds": [b["place_id"] for b in self.cfg["branches"]],
            "maxReviews": a.get("max_reviews_per_run", 300),
            "reviewsSort": "newest",
            # Absolute dates are interpreted as UTC by the actor.
            "reviewsStartDate": iso(since).rstrip("Z"),
            "language": a.get("language", "ar"),
            "reviewsOrigin": "google",
            # Keep reviewer names out of the (public) dashboard data.
            "personalData": False,
        }
        url = (f"https://api.apify.com/v2/acts/{a['actor']}"
               f"/run-sync-get-dataset-items?timeout=290&format=json")
        log(f"Apify: {len(body['placeIds'])} places since {body['reviewsStartDate']}")
        r = requests.post(url, json=body, timeout=320,
                          headers={"Authorization": f"Bearer {self.token}"})
        if not r.ok:
            raise RuntimeError(f"Apify {r.status_code}: {r.text[:300]}")
        items = r.json()
        out = []
        for it in items:
            branch = self.cfg["_by_place"].get(it.get("placeId"))
            rid = it.get("reviewId")
            if not branch or not rid:
                continue
            out.append(_normalize(
                branch, source=self.name, rid=rid,
                stars=it.get("stars") or it.get("rating"),
                text=it.get("text"),
                translated=it.get("textTranslated"),
                published=it.get("publishedAtDate"),
                url=it.get("reviewUrl") or it.get("url"),
                reply=it.get("responseFromOwnerText"),
            ))
        log(f"Apify returned {len(items)} items, {len(out)} matched branches")
        return out


class GBPSource:
    """Official Business Profile API (needs approved Basic API Access)."""
    name = "gbp"

    def __init__(self, cfg):
        self.cfg = cfg
        tok = requests.post("https://oauth2.googleapis.com/token", data={
            "client_id": env("GBP_CLIENT_ID", required=True),
            "client_secret": env("GBP_CLIENT_SECRET", required=True),
            "refresh_token": env("GBP_REFRESH_TOKEN", required=True),
            "grant_type": "refresh_token",
        }, timeout=30)
        tok.raise_for_status()
        self.h = {"Authorization": f"Bearer {tok.json()['access_token']}"}

    def _get(self, url, params=None):
        r = requests.get(url, headers=self.h, params=params, timeout=60)
        if not r.ok:
            raise RuntimeError(f"GBP {r.status_code}: {r.text[:300]}")
        return r.json()

    @staticmethod
    def _auto_branch(loc):
        """A location in your account that isn't in config.json yet."""
        title = loc.get("title", "")
        brand = "ارميما" if ("ميما" in title or "mima" in title.lower()) else (
            "رشف" if ("رشف" in title or "rshaf" in title.lower()) else title)
        addr = loc.get("storefrontAddress") or {}
        city = addr.get("locality") or ""
        line = (addr.get("addressLines") or [""])[0]
        return {"id": "gbp-" + loc["name"].split("/")[-1], "brand": brand,
                "name": " - ".join(x for x in (city, line) if x) or title,
                "city": city, "supervisor_chat_ids": []}

    def _locations(self):
        """Every location in your Business Profile account → branch.
        Configured branches are matched by placeId; new ones are picked up automatically."""
        found = {}
        accts = self._get("https://mybusinessaccountmanagement.googleapis.com/v1/accounts")
        for acct in accts.get("accounts", []):
            token = None
            while True:
                data = self._get(
                    f"https://mybusinessbusinessinformation.googleapis.com/v1/{acct['name']}/locations",
                    {"readMask": "name,title,metadata,storefrontAddress", "pageSize": 100, "pageToken": token})
                for loc in data.get("locations", []):
                    pid = (loc.get("metadata") or {}).get("placeId")
                    branch = self.cfg["_by_place"].get(pid)
                    if branch is None:
                        branch = self._auto_branch(loc)
                        log(f"GBP: location not in config, auto-added as {branch['id']} ({branch['brand']} {branch['name']}, placeId={pid})")
                    found[f"{acct['name']}/{loc['name']}"] = branch
                token = data.get("nextPageToken")
                if not token:
                    break
        return found

    def fetch(self, since):
        out = []
        for path, branch in self._locations().items():
            token = None
            while True:
                data = self._get(f"https://mybusiness.googleapis.com/v4/{path}/reviews",
                                 {"pageSize": 50, "orderBy": "updateTime desc", "pageToken": token})
                stop = False
                for rv in data.get("reviews", []):
                    upd = parse_iso(rv.get("updateTime"))
                    if upd and upd < since:
                        stop = True
                        break
                    out.append(_normalize(
                        branch, source=self.name, rid=rv["reviewId"],
                        stars=STAR_WORDS.get(rv.get("starRating")),
                        text=rv.get("comment"),
                        published=rv.get("createTime"),
                        reply=(rv.get("reviewReply") or {}).get("comment"),
                    ))
                token = data.get("nextPageToken")
                if stop or not token:
                    break
        log(f"GBP returned {len(out)} reviews")
        return out


def get_source(cfg):
    return {"apify": ApifySource, "gbp": GBPSource}[cfg.get("source", "apify")](cfg)

# ======================================================================
# analyze
# ======================================================================
"""Classify reviews with Claude (topics, sentiment, severity, staff mentions)."""
import json
import re
import time

import requests


TOPICS = {
    "taste": "الطعم والجودة",
    "speed": "السرعة والانتظار",
    "staff": "تعامل الموظفين",
    "accuracy": "دقة الطلب",
    "price": "السعر والقيمة",
    "hygiene": "النظافة وسلامة الغذاء",
    "facility": "المكان والدرايف ثرو والمواقف",
    "availability": "توفر المنتجات",
    "hours": "أوقات العمل والإغلاق",
    "payment": "الدفع والتطبيقات",
    "other": "أخرى",
}

SYSTEM = f"""أنت محلل تجربة عملاء لمقاهي ومطاعم في منطقة القصيم بالسعودية:
- "رشف": مقهى درايف ثرو (قهوة مختصة، مشروبات باردة، حلويات ودونات من معمل الشركة).
- "ارميما": مطعم برجر.

لكل تعليق أعد كائن JSON بالحقول التالية بالضبط:
- "id": نفس المعرّف كما هو
- "sentiment": "positive" أو "neutral" أو "negative"
- "topics": قائمة من هذه المفاتيح فقط تذكر المواضيع المطروحة فعلاً: {list(TOPICS)}
- "negative_topics": المواضيع التي اشتكى منها العميل فقط (قائمة فرعية من topics)
- "severity": "none" للإيجابي، أو "low" / "medium" / "high" / "critical"
   (critical = سلامة غذاء، جسم غريب، تسمم، إساءة أو صراخ من موظف، مخالفة نظامية)
- "summary": ملخص عربي في سطر واحد قصير (أقل من 15 كلمة)
- "staff_mentions": قائمة بأسماء الموظفين المذكورين: [{{"name": "الاسم كما كُتب", "sentiment": "positive|negative"}}]
- "products": أسماء المنتجات المذكورة (قائمة نصوص قصيرة)
- "action": إجراء عملي مقترح للمشرف بجملة واحدة إذا كان فيه شكوى، وإلا ""

قواعد: التعليق بدون نص يُحكم عليه من النجوم فقط. لا تخترع معلومات غير موجودة.
أعد مصفوفة JSON فقط، بدون أي نص قبلها أو بعدها."""


def _fallback(r):
    s = r.get("stars") or 3
    sentiment = "positive" if s >= 4 else ("neutral" if s == 3 else "negative")
    return {
        "sentiment": sentiment, "topics": [], "negative_topics": [],
        "severity": "none" if s >= 4 else ("low" if s == 3 else "medium"),
        "summary": "", "staff_mentions": [], "products": [], "action": "",
        "model": "stars-only",
    }


def _extract_json(text):
    m = re.search(r"\[.*\]", text, re.S)
    return json.loads(m.group(0) if m else text)


def _call_claude(batch, model):
    payload = [
        {"id": r["id"], "brand": r["brand"], "branch": r["branch_name"],
         "stars": r["stars"], "text": r["text"] or r.get("text_translated") or ""}
        for r in batch
    ]
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": env("ANTHROPIC_API_KEY", required=True),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 8000,
            "system": SYSTEM,
            "messages": [{"role": "user",
                          "content": json.dumps(payload, ensure_ascii=False)}],
        },
        timeout=180,
    )
    if resp.status_code in (429, 529) or resp.status_code >= 500:
        time.sleep(20)  # rate limit / overloaded: wait once, then let caller split/retry
        raise RuntimeError(f"Claude {resp.status_code}: {resp.text[:200]}")
    if not resp.ok:
        raise RuntimeError(f"Claude {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    if body.get("stop_reason") == "max_tokens":
        raise RuntimeError("Claude reply truncated (max_tokens)")
    text = "".join(b.get("text", "") for b in body["content"])
    return {str(x["id"]): x for x in _extract_json(text)}


def _classify(batch, model, depth=0):
    """Classify a batch; on failure split it in half so one bad batch never
    sinks the rest. Returns {id: analysis} for whatever succeeded."""
    try:
        return _call_claude(batch, model)
    except Exception as e:
        log(f"Claude batch of {len(batch)} failed: {e}")
        if len(batch) <= 1 or depth >= 4:
            return {}
        mid = len(batch) // 2
        out = _classify(batch[:mid], model, depth + 1)
        out.update(_classify(batch[mid:], model, depth + 1))
        return out


ANALYSIS_VERSION = 2  # bump to re-run fallback-only reviews with improved logic


def analyze_pending(store, cfg):
    use_claude = bool(env("ANTHROPIC_API_KEY"))

    def needs(r):
        a = r.get("analysis")
        if not a:
            return True
        if not use_claude or a.get("model") != "stars-only":
            return False
        # Retry reviews that only got the stars fallback (e.g. API was down).
        if a.get("v", 1) < ANALYSIS_VERSION:
            return True
        return a.get("attempts", 0) < 3

    pending = [r for r in store["reviews"] if needs(r)]
    if not pending:
        return 0
    model = cfg["claude"]["model"]
    # Arabic JSON replies are long; keep batches small so they never get cut off.
    size = min(cfg["claude"].get("batch_size", 8), 8)
    done = 0
    for i in range(0, len(pending), size):
        batch = pending[i:i + size]
        results = _classify(batch, model) if use_claude else {}
        for r in batch:
            a = results.get(r["id"])
            if a:
                a.pop("id", None)
                a["topics"] = [t for t in a.get("topics", []) if t in TOPICS]
                a["negative_topics"] = [t for t in a.get("negative_topics", []) if t in TOPICS]
                a["model"] = model
                r["analysis"] = a
            else:
                old = r.get("analysis") or {}
                prev = old.get("attempts", 0) if old.get("v", 1) == ANALYSIS_VERSION else 0
                r["analysis"] = _fallback(r)
                r["analysis"]["attempts"] = prev + (1 if use_claude else 0)
                r["analysis"]["v"] = ANALYSIS_VERSION
            done += 1
    ok = sum(1 for r in pending if (r.get("analysis") or {}).get("model") != "stars-only")
    log(f"Analyzed {done} reviews: {ok} by Claude, {done - ok} stars-only (claude={'on' if use_claude else 'off'})")
    return done

# ======================================================================
# notify
# ======================================================================
"""Telegram notifications: instant alerts, daily digest, weekly report."""
import json
from collections import Counter, defaultdict
from datetime import timedelta

import requests


STAR = "⭐"
SEV_AR = {"critical": "🚨 حرج", "high": "🔴 عالي", "medium": "🟠 متوسط",
          "low": "🟡 منخفض", "none": "—"}


def _a(r):
    return r.get("analysis") or {}


def _is_alert(r, cfg):
    rules = cfg["alerts"]
    s = r.get("stars") or 0
    return (s and s <= rules["max_stars"]) or _a(r).get("severity") in rules["also_alert_severity"]


def _review_block(r, cfg, with_action=True):
    a = _a(r)
    lines = [
        f"<b>{esc(r['brand'])} | {esc(r['branch_name'])}</b>",
        f"{STAR * (r.get('stars') or 0)}  ({r.get('stars')}/5)   الخطورة: {SEV_AR.get(a.get('severity'), '—')}",
    ]
    pub = parse_iso(r.get("published_at"))
    if pub:
        lines.append(f"🕒 {local_str(pub, cfg)}")
    text = r.get("text") or r.get("text_translated")
    lines.append(f"💬 {esc(text[:700]) if text else '<i>بدون نص — تقييم بالنجوم فقط</i>'}")
    if a.get("summary"):
        lines.append(f"📝 {esc(a['summary'])}")
    if a.get("negative_topics"):
        lines.append("🏷 " + "، ".join(TOPICS[t] for t in a["negative_topics"]))
    if with_action and a.get("action"):
        lines.append(f"✅ المقترح: {esc(a['action'])}")
    if r.get("url"):
        lines.append(f'<a href="{esc(r["url"])}">فتح التعليق في قوقل</a>')
    return "\n".join(lines)


# ---------- instant ----------
def send_alerts(store, cfg):
    sent = 0
    for r in store["reviews"]:
        if r.get("alerted") or not r.get("analysis"):
            continue
        if not _is_alert(r, cfg):
            r["alerted"] = "skip"
            continue
        branch = cfg["_by_id"].get(r["branch_id"], {})
        msg = "🔔 <b>تعليق يحتاج انتباه</b>\n\n" + _review_block(r, cfg)
        telegram_send(owner_chats() + branch.get("supervisor_chat_ids", []), msg)
        r["alerted"] = True
        sent += 1
    log(f"Alerts sent: {sent}")
    return sent


def mark_existing_as_alerted(store):
    """On the very first backfill we don't want 90 days of alerts."""
    for r in store["reviews"]:
        r.setdefault("alerted", "backfill")


# ---------- stats ----------
def _window(store, days):
    since = now_utc() - timedelta(days=days)
    return [r for r in store["reviews"]
            if (parse_iso(r.get("published_at")) or since) >= since]


def _stats(reviews):
    stars = [r["stars"] for r in reviews if r.get("stars")]
    avg = sum(stars) / len(stars) if stars else 0
    neg = [r for r in reviews if _a(r).get("sentiment") == "negative"]
    topics = Counter(t for r in neg for t in _a(r).get("negative_topics", []))
    staff_pos, staff_neg = Counter(), Counter()
    for r in reviews:
        for m in _a(r).get("staff_mentions", []):
            name = (m.get("name") or "").strip()
            if name:
                (staff_pos if m.get("sentiment") == "positive" else staff_neg)[(name, r["branch_name"])] += 1
    by_branch = defaultdict(list)
    for r in reviews:
        by_branch[(r["brand"], r["branch_name"])].append(r)
    return {"count": len(reviews), "avg": avg, "neg": neg, "topics": topics,
            "staff_pos": staff_pos, "staff_neg": staff_neg, "by_branch": by_branch}


def _branch_lines(by_branch):
    rows = []
    for (brand, name), rs in by_branch.items():
        st = [r["stars"] for r in rs if r.get("stars")]
        avg = sum(st) / len(st) if st else 0
        neg = sum(1 for r in rs if _a(r).get("sentiment") == "negative")
        rows.append((avg, f"• {esc(brand)} {esc(name)}: {avg:.1f}{STAR} ({len(rs)} تعليق، {neg} سلبي)"))
    return [line for _, line in sorted(rows)]


# ---------- daily ----------
def send_daily(store, cfg):
    day = _window(store, 1)
    s = _stats(day)
    lines = [f"📊 <b>ملخص تعليقات قوقل — {local_str(now_utc(), cfg, '%Y-%m-%d')}</b>", ""]
    if not day:
        lines.append("ما وصل تعليقات جديدة خلال آخر 24 ساعة.")
    else:
        lines += [f"عدد التعليقات: <b>{s['count']}</b>   المتوسط: <b>{s['avg']:.2f}</b>{STAR}",
                  f"السلبية: <b>{len(s['neg'])}</b>", "", "<b>حسب الفرع</b>"]
        lines += _branch_lines(s["by_branch"])
        if s["topics"]:
            lines += ["", "<b>أبرز الشكاوى</b>"]
            lines += [f"• {TOPICS[t]}: {n}" for t, n in s["topics"].most_common(4)]
        if s["staff_pos"]:
            lines += ["", "<b>موظفين انشكروا بالاسم 👏</b>"]
            lines += [f"• {esc(n)} ({esc(b)}) ×{c}" for (n, b), c in s["staff_pos"].most_common(5)]
    lines += ["", f'<a href="{cfg["dashboard_url"]}">فتح لوحة المتابعة</a>']
    telegram_send(owner_chats(), "\n".join(lines))


# ---------- weekly ----------
def _claude_report(cfg, payload):
    key = env("ANTHROPIC_API_KEY")
    if not key:
        return None
    prompt = (
        "أنت مستشار تشغيل لمقهى درايف ثرو (رشف) ومطعم برجر (ارميما) في القصيم. "
        "هذه بيانات تعليقات قوقل للأسبوع الماضي مقارنة بالأسبوع الذي قبله. "
        "اكتب تقرير أسبوعي مختصر لصاحب الشركة باللهجة السعودية المهنية، بنص عادي يصلح لتيليجرام "
        "(بدون Markdown، استخدم • للنقاط وأسطر فارغة بين الأقسام)، لا يتجاوز 300 كلمة، بالأقسام:\n"
        "1) الخلاصة في سطرين\n2) أكثر 3 مشاكل متكررة مع الفروع المعنية\n"
        "3) فرع يحتاج تدخل عاجل ولماذا\n4) نقاط قوة وموظفين يستحقون التقدير\n"
        "5) 3 إجراءات محددة لهذا الأسبوع.\n"
        "اعتمد على الأرقام فقط ولا تخترع شيئاً.\n\nالبيانات:\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": cfg["claude"].get("report_model", cfg["claude"]["model"]),
              "max_tokens": 1500,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=120,
    )
    if not r.ok:
        log(f"Weekly report Claude error: {r.status_code} {r.text[:200]}")
        return None
    return "".join(b.get("text", "") for b in r.json()["content"]).strip()


def send_weekly(store, cfg):
    week = _window(store, 7)
    prev = [r for r in _window(store, 14) if r not in week]
    s, p = _stats(week), _stats(prev)

    def branch_summary(stats):
        out = {}
        for (brand, name), rs in stats["by_branch"].items():
            st = [r["stars"] for r in rs if r.get("stars")]
            out[f"{brand} - {name}"] = {
                "count": len(rs),
                "avg": round(sum(st) / len(st), 2) if st else None,
                "negative": sum(1 for r in rs if _a(r).get("sentiment") == "negative"),
                "complaints": dict(Counter(t for r in rs for t in _a(r).get("negative_topics", []))),
            }
        return out

    payload = {
        "this_week": {"count": s["count"], "avg": round(s["avg"], 2),
                      "negative": len(s["neg"]),
                      "complaints": {TOPICS[k]: v for k, v in s["topics"].items()},
                      "branches": branch_summary(s),
                      "staff_praised": [f"{n} ({b}) x{c}" for (n, b), c in s["staff_pos"].most_common(8)],
                      "staff_complained": [f"{n} ({b}) x{c}" for (n, b), c in s["staff_neg"].most_common(8)],
                      "negative_samples": [
                          f"[{r['brand']} {r['branch_name']} {r['stars']}★] {_a(r).get('summary') or (r.get('text') or '')[:150]}"
                          for r in s["neg"][:40]]},
        "last_week": {"count": p["count"], "avg": round(p["avg"], 2), "negative": len(p["neg"])},
    }
    body = _claude_report(cfg, payload)
    head = f"🗓 <b>التقرير الأسبوعي لتعليقات قوقل</b>\nالأسبوع: {s['count']} تعليق، متوسط {s['avg']:.2f}{STAR} (الأسبوع السابق {p['avg']:.2f}{STAR})\n"
    if body:
        msg = head + "\n" + esc(body)
    else:  # no API key: numbers only
        msg = head + "\n" + "\n".join(_branch_lines(s["by_branch"]))
    msg += f'\n\n<a href="{cfg["dashboard_url"]}">فتح لوحة المتابعة</a>'
    telegram_send(owner_chats(), msg)

# ======================================================================
# run
# ======================================================================
"""Entry point.

python monitor.py --mode collect   # every 2h: fetch → analyze → alert
python monitor.py --mode daily     # collect + daily digest
python monitor.py --mode weekly    # collect + weekly report
python monitor.py --mode analyze   # (re)analyze + alert only, no fetch
"""
import argparse
from datetime import timedelta


KEEP_FIELDS_ON_UPDATE = ("analysis", "alerted", "collected_at")


def collect(store, state, cfg):
    started = now_utc()
    last = parse_iso(state.get("last_success_utc"))
    first_run = last is None
    if first_run:
        since = started - timedelta(days=cfg.get("first_run_backfill_days", 90))
    else:
        since = last - timedelta(hours=cfg["apify"].get("overlap_hours", 6))

    fetched = get_source(cfg).fetch(since)

    by_id = {r["id"]: r for r in store["reviews"]}
    new = 0
    for r in fetched:
        old = by_id.get(r["id"])
        if old is None:
            r["collected_at"] = iso(started)
            store["reviews"].append(r)
            by_id[r["id"]] = r
            new += 1
        else:
            # Review edited by customer or owner replied → refresh content.
            changed = (_clean(old.get("text")) != r["text"] or old.get("stars") != r["stars"])
            for k, v in r.items():
                if k not in KEEP_FIELDS_ON_UPDATE:
                    old[k] = v
            if changed:
                old["analysis"] = None
                old["alerted"] = None
                old["edited"] = True

    if first_run:
        mark_existing_as_alerted(store)
    state["last_success_utc"] = iso(started)
    state["last_source"] = cfg.get("source")
    log(f"Collected {len(fetched)} (new {new}, backfill={first_run})")
    return new, first_run


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="collect",
                    choices=["collect", "daily", "weekly", "analyze"])
    args = ap.parse_args()

    cfg = load_config()
    store, state = load_store(), load_state()

    try:
        if args.mode != "analyze":
            new, first_run = collect(store, state, cfg)
            save_store(store)
            save_state(state)
        analyze_pending(store, cfg)
        save_store(store)
        send_alerts(store, cfg)
        save_store(store)
    except Exception as e:
        # Tell the owner once if something breaks, then fail the job.
        fails = state.get("consecutive_failures", 0) + 1
        state["consecutive_failures"] = fails
        save_state(state)
        if fails in (2, 10):
            telegram_send(owner_chats(),
                          f"⚠️ نظام متابعة التعليقات فشل {fails} مرات متتالية:\n<code>{str(e)[:300]}</code>")
        raise
    state["consecutive_failures"] = 0
    save_state(state)

    if args.mode == "daily":
        send_daily(store, cfg)
    elif args.mode == "weekly":
        send_weekly(store, cfg)


if __name__ == "__main__":
    main()
