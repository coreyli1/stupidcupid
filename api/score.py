"""
api/score.py  —  Vercel serverless function
Reads generated profiles from the sheet, scores them, and writes results back.

Env vars required (set in Vercel dashboard):
  GOOGLE_SERVICE_ACCOUNT  — full JSON of the service account key file (as a string)
  MATCH_CONFIG            — JSON string of your match_config.json contents
"""

import json
import os
from datetime import datetime
from http.server import BaseHTTPRequestHandler

from google.oauth2 import service_account
from googleapiclient.discovery import build

SHEET_ID   = "1grXXFLQ7LQwA5gNwcEzVOM0y-h1gpeDxijsBsko3Pzc"
SOURCE_TAB = "Generated Profiles"
OUTPUT_TAB = "Match Scores"
SCOPES     = ["https://www.googleapis.com/auth/spreadsheets"]


# ── Auth ──────────────────────────────────────────────────────────────
def get_service():
    sa_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT"])
    creds   = service_account.Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


def get_config():
    raw = os.environ.get("MATCH_CONFIG", "")
    if not raw:
        raise ValueError("MATCH_CONFIG env var is not set. Add it in the Vercel dashboard.")
    cfg = json.loads(raw)
    return {k: v for k, v in cfg.items() if not k.startswith("_")}


# ── Read profiles ─────────────────────────────────────────────────────
def read_profiles(service):
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{SOURCE_TAB}'"
    ).execute()
    rows = result.get("values", [])

    all_idx = next(
        (i for i, r in enumerate(rows) if r and "ALL" in r[0] and "GENERATED PROFILES" in r[0]),
        -1,
    )
    if all_idx < 0:
        return [], []

    fields   = (rows[all_idx + 1] or [])[1:]
    profiles = []
    for row in rows[all_idx + 2:]:
        if not row or not row[0]:
            continue
        p = {fields[j]: (row[j + 1] if j + 1 < len(row) else "") for j in range(len(fields))}
        profiles.append(p)
    return profiles, fields


# ── Scoring ───────────────────────────────────────────────────────────
def score_field(value, rule):
    weight  = float(rule.get("weight", 0))
    ctype   = rule.get("condition_type", "")
    target  = rule.get("value")
    partial = rule.get("partial_credit", False)
    val     = str(value).strip()

    if not val:
        return 0.0, weight, "no value"

    if ctype in ("less_than", "greater_than", "between"):
        try:
            num = float(val)
        except ValueError:
            return 0.0, weight, f"'{val}' is not a number"

        if ctype == "less_than":
            t   = float(target)
            hit = num < t
            s   = weight * (1 - num / t) if (hit and partial) else (weight if hit else 0.0)
            return s, weight, f"{num} < {t}" if hit else f"{num} >= {t}"

        if ctype == "greater_than":
            t   = float(target)
            hit = num > t
            return (weight if hit else 0.0), weight, f"{num} > {t}" if hit else f"{num} <= {t}"

        if ctype == "between":
            lo, hi = float(target[0]), float(target[1])
            hit    = lo <= num <= hi
            if hit:
                s = weight
            elif partial:
                dist = min(abs(num - lo), abs(num - hi))
                s    = max(0.0, weight * (1 - dist / (hi - lo)))
            else:
                s = 0.0
            return s, weight, f"{num} in [{lo},{hi}]" if hit else f"{num} outside [{lo},{hi}]"

    if ctype == "equal_to":
        hit = val.lower() == str(target).lower()
        return (weight if hit else 0.0), weight, f"== {target}" if hit else f"!= {target}"

    if ctype == "in_list":
        hit = val.lower() in [str(v).lower() for v in target]
        return (weight if hit else 0.0), weight, "in list" if hit else "not in list"

    if ctype == "compatible_with":
        for group in rule.get("groups", []):
            if val.lower() in [g.lower() for g in group]:
                return weight, weight, "compatible group"
        return 0.0, weight, "not compatible"

    return 0.0, weight, f"unknown condition '{ctype}'"


def score_all(profiles, config):
    scored = []
    for i, p in enumerate(profiles, 1):
        total, max_total, breakdown = 0.0, 0.0, {}
        for field, rule in config.items():
            earned, max_pts, expl = score_field(p.get(field, ""), rule)
            total     += earned
            max_total += max_pts
            breakdown[field] = {
                "earned":      round(earned, 2),
                "max":         round(max_pts, 2),
                "hit":         earned >= max_pts and max_pts > 0,
                "explanation": expl,
            }
        pct = (total / max_total * 100) if max_total > 0 else 0
        scored.append({
            "rank":     None,
            "num":      i,
            "profile":  p,
            "score":    round(total, 2),
            "maxScore": round(max_total, 2),
            "matchPct": round(pct, 1),
            "breakdown": breakdown,
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    for i, e in enumerate(scored, 1):
        e["rank"] = i
    return scored


# ── Write scores ──────────────────────────────────────────────────────
def write_scores(service, scored, config, fields):
    meta     = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    existing = {s["properties"]["title"] for s in meta["sheets"]}
    if OUTPUT_TAB not in existing:
        service.spreadsheets().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": OUTPUT_TAB}}}]},
        ).execute()

    service.spreadsheets().values().clear(
        spreadsheetId=SHEET_ID, range=f"'{OUTPUT_TAB}'"
    ).execute()

    timestamp   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bdown_keys  = list(config.keys())
    rows = [
        [f"Match Scores — Generated: {timestamp}"],
        [f"{len(scored)} profiles scored"],
        [],
        ["FIELD WEIGHTS & CONDITIONS"],
        ["Field", "Weight", "Condition", "Target"],
    ]
    for field, rule in config.items():
        rows.append([field, rule.get("weight",""), rule.get("condition_type",""),
                     str(rule.get("value") or rule.get("groups",""))])
    rows += [[], ["RANKED PROFILES"],
             ["Rank","Score","Max Score","Match %"] + fields + [f"{f} ✓/✗" for f in bdown_keys]]

    for e in scored:
        bd_cells = []
        for f in bdown_keys:
            bd   = e["breakdown"].get(f, {})
            mark = "✓" if bd.get("hit") else "✗"
            cell = f"{mark} {bd.get('earned',0)}/{bd.get('max',0)}"
            if not bd.get("hit"):
                cell += f" ({bd.get('explanation','')})"
            bd_cells.append(cell)
        rows.append(
            [e["rank"], e["score"], e["maxScore"], f"{e['matchPct']}%"]
            + [e["profile"].get(f, "") for f in fields]
            + bd_cells
        )

    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"'{OUTPUT_TAB}'!A1",
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()


# ── Handler ───────────────────────────────────────────────────────────
class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        try:
            config             = get_config()
            service            = get_service()
            profiles, fields   = read_profiles(service)
            if not profiles:
                raise ValueError("No profiles found — run Generate first.")
            scored = score_all(profiles, config)
            write_scores(service, scored, config, fields)

            self._respond(200, {"scored": scored, "fields": fields})
        except Exception as e:
            self._respond(500, {"error": str(e)})

    def _respond(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, *args):
        pass
