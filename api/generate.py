"""
api/generate.py  —  Vercel serverless function
Generates mix-and-match dating profiles and writes them to the Google Sheet.

Env vars required (set in Vercel dashboard):
  GOOGLE_SERVICE_ACCOUNT  — full JSON of the service account key file (as a string)
"""

import json
import os
import random
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from io import StringIO
from urllib.parse import parse_qs, urlparse

import pandas as pd
import requests as req_lib
from google.oauth2 import service_account
from googleapiclient.discovery import build

SHEET_ID   = "1grXXFLQ7LQwA5gNwcEzVOM0y-h1gpeDxijsBsko3Pzc"
SOURCE_GID = "0"
OUTPUT_TAB = "Generated Profiles"
SCOPES     = ["https://www.googleapis.com/auth/spreadsheets"]


# ── Auth ──────────────────────────────────────────────────────────────
def get_service():
    sa_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT"])
    creds   = service_account.Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


# ── Source data ───────────────────────────────────────────────────────
def load_source_csv():
    url  = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SOURCE_GID}"
    resp = req_lib.get(url, timeout=15)
    resp.raise_for_status()
    return pd.read_csv(StringIO(resp.text))


def column_pools(df):
    pools = {}
    for col in df.columns:
        vals = [v for v in df[col].dropna().tolist() if str(v).strip()]
        if vals:
            pools[col] = vals
    return pools


# ── Generation ────────────────────────────────────────────────────────
def generate_profile(pools):
    return {f: random.choice(v) for f, v in pools.items()}


# ── Write to sheet ────────────────────────────────────────────────────
def ensure_tab(service, name):
    meta     = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    existing = {s["properties"]["title"] for s in meta["sheets"]}
    if name not in existing:
        service.spreadsheets().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": name}}}]},
        ).execute()


def write_profiles(service, profiles, main_profile, fields):
    ensure_tab(service, OUTPUT_TAB)
    service.spreadsheets().values().clear(
        spreadsheetId=SHEET_ID, range=f"'{OUTPUT_TAB}'"
    ).execute()

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        [f"Generated: {timestamp}", f"{len(profiles)} profiles created"],
        [],
        ["★ MAIN PROFILE (selected) ★"],
        fields,
        [str(main_profile.get(f, "")) for f in fields],
        [],
        [f"ALL {len(profiles)} GENERATED PROFILES"],
        ["#"] + fields,
    ]
    for i, p in enumerate(profiles, 1):
        rows.append([str(i)] + [str(p.get(f, "")) for f in fields])

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
            params = parse_qs(urlparse(self.path).query)
            num    = int(params.get("num",  ["5"])[0])
            seed   = params.get("seed", [None])[0]
            if seed:
                random.seed(int(seed))

            df     = load_source_csv()
            pools  = column_pools(df)
            fields = list(pools.keys())

            profiles     = [generate_profile(pools) for _ in range(num)]
            main_profile = random.choice(profiles)

            service = get_service()
            write_profiles(service, profiles, main_profile, fields)

            self._respond(200, {
                "profiles":    profiles,
                "mainProfile": main_profile,
                "fields":      fields,
            })
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
