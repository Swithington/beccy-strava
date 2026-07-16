#!/usr/bin/env python3
"""
strava_fetch.py — Fetch Strava activities + 100m splits from GPS streams.

Credentials are read from environment variables (set as GitHub Secrets):
    STRAVA_CLIENT_ID
    STRAVA_CLIENT_SECRET
    STRAVA_REFRESH_TOKEN

Or falls back to strava_tokens.json for local runs.

Usage:
    python strava_fetch.py              # incremental (new activities only)
    python strava_fetch.py --all        # re-fetch everything from scratch
    python strava_fetch.py --days 30    # fetch last 30 days
"""

import argparse
import csv
import json
import os
import time
from datetime import datetime
from pathlib import Path

import requests

TOKEN_FILE      = "strava_tokens.json"
ACTIVITIES_FILE = "strava_activities.csv"
SPLITS_FILE     = "strava_splits_100m.csv"
STATE_FILE      = "strava_fetch_state.json"
BASE_URL        = "https://www.strava.com/api/v3"
SPLIT_INTERVAL  = 100  # metres

ACTIVITY_FIELDS = [
    "id", "name", "type", "sport_type", "start_date_local",
    "distance", "moving_time", "elapsed_time",
    "total_elevation_gain", "elev_high", "elev_low",
    "average_speed", "max_speed",
    "average_heartrate", "max_heartrate",
    "average_cadence", "average_watts",
    "suffer_score", "calories",
    "start_latlng", "end_latlng",
    "achievement_count", "kudos_count",
    "trainer", "commute", "manual",
    "gear_id", "description",
]


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

def load_credentials():
    """Load from env vars (GitHub Actions) or fall back to local token file."""
    client_id     = os.environ.get("STRAVA_CLIENT_ID")
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET")
    refresh_token = os.environ.get("STRAVA_REFRESH_TOKEN")

    if client_id and client_secret and refresh_token:
        print("Using credentials from environment variables.")
        return {
            "client_id":     client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "access_token":  None,
            "expires_at":    0,
        }

    if Path(TOKEN_FILE).exists():
        print("Using credentials from strava_tokens.json.")
        with open(TOKEN_FILE) as f:
            return json.load(f)

    raise FileNotFoundError(
        "No credentials found. Set STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, "
        "STRAVA_REFRESH_TOKEN env vars, or run strava_auth.py to create strava_tokens.json."
    )


def get_access_token(tokens):
    """Always refresh — safe to call every run."""
    print("Getting fresh access token...")
    resp = requests.post("https://www.strava.com/oauth/token", data={
        "client_id":     tokens["client_id"],
        "client_secret": tokens["client_secret"],
        "grant_type":    "refresh_token",
        "refresh_token": tokens["refresh_token"],
    })
    resp.raise_for_status()
    new = resp.json()
    print(f"✓ Access token valid until {datetime.fromtimestamp(new['expires_at']).strftime('%H:%M')}")
    return new["access_token"]


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def api_get(endpoint, access_token, params=None, retries=3):
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"{BASE_URL}{endpoint}"
    for attempt in range(retries):
        resp = requests.get(url, headers=headers, params=params or {})
        if resp.status_code == 429:
            wait = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60)) - int(time.time())
            wait = max(wait, 10)
            print(f"  Rate limited. Waiting {wait}s...")
            time.sleep(wait)
            continue
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        time.sleep(0.25)
        return resp.json()
    return None


# ---------------------------------------------------------------------------
# Fetch activity list
# ---------------------------------------------------------------------------

def fetch_activity_list(access_token, after_epoch=None):
    activities = []
    page = 1
    while True:
        params = {"per_page": 100, "page": page}
        if after_epoch:
            params["after"] = after_epoch
        data = api_get("/athlete/activities", access_token, params)
        if not data:
            break
        activities.extend(data)
        print(f"  Page {page}: {len(data)} activities (total: {len(activities)})")
        page += 1
    return activities


# ---------------------------------------------------------------------------
# Fetch streams + build 100m splits
# ---------------------------------------------------------------------------

STREAM_KEYS = "time,distance,latlng,altitude,heartrate,cadence,velocity_smooth,moving"

def fetch_streams(activity_id, access_token):
    return api_get(
        f"/activities/{activity_id}/streams",
        access_token,
        params={"keys": STREAM_KEYS, "key_by_type": "true"}
    ) or {}


def interpolate(series, target_dist, dist_series):
    for i in range(1, len(dist_series)):
        d0, d1 = dist_series[i-1], dist_series[i]
        if d0 <= target_dist <= d1:
            if d1 == d0:
                return series[i]
            frac = (target_dist - d0) / (d1 - d0)
            v0, v1 = series[i-1], series[i]
            if v0 is None or v1 is None:
                return None
            return v0 + frac * (v1 - v0)
    return None


def build_100m_splits(activity_id, start_date_local, streams):
    if "distance" not in streams:
        return []

    dist_data = streams["distance"]["data"]
    time_data = streams.get("time",             {}).get("data")
    hr_data   = streams.get("heartrate",        {}).get("data")
    alt_data  = streams.get("altitude",         {}).get("data")
    cad_data  = streams.get("cadence",          {}).get("data")
    vel_data  = streams.get("velocity_smooth",  {}).get("data")
    latlng    = streams.get("latlng",           {}).get("data")

    total_dist = dist_data[-1] if dist_data else 0
    splits = []
    split_num = 1
    target = SPLIT_INTERVAL

    while target <= total_dist + SPLIT_INTERVAL:
        seg_start = target - SPLIT_INTERVAL
        seg_end   = min(target, total_dist)
        if seg_start >= total_dist:
            break

        seg_dist = seg_end - seg_start

        # Time for segment
        seg_time_s = None
        if time_data:
            t0 = interpolate(time_data, seg_start, dist_data)
            t1 = interpolate(time_data, seg_end,   dist_data)
            if t0 is not None and t1 is not None:
                seg_time_s = t1 - t0

        # Average HR
        avg_hr = None
        if hr_data:
            in_seg = [hr_data[i] for i in range(len(dist_data))
                      if seg_start <= dist_data[i] <= seg_end and hr_data[i] is not None]
            avg_hr = round(sum(in_seg) / len(in_seg), 1) if in_seg else None

        # Average cadence
        avg_cad = None
        if cad_data:
            in_seg = [cad_data[i] for i in range(len(dist_data))
                      if seg_start <= dist_data[i] <= seg_end and cad_data[i] is not None]
            avg_cad = round(sum(in_seg) / len(in_seg), 1) if in_seg else None

        # Average velocity → pace
        avg_vel = pace_min_km = pace_str = None
        if vel_data:
            in_seg = [vel_data[i] for i in range(len(dist_data))
                      if seg_start <= dist_data[i] <= seg_end and vel_data[i] is not None]
            if in_seg:
                avg_vel = sum(in_seg) / len(in_seg)
                if avg_vel > 0:
                    pace_min_km = round(1000 / avg_vel / 60, 4)
                    p = pace_min_km
                    pace_str = f"{int(p)}:{int((p % 1) * 60):02d}"

        # Elevation
        alt_start = interpolate(alt_data, seg_start, dist_data) if alt_data else None
        alt_end   = interpolate(alt_data, seg_end,   dist_data) if alt_data else None
        elev_change = round(alt_end - alt_start, 2) if alt_start is not None and alt_end is not None else None

        # GPS position
        start_lat = start_lng = None
        if latlng:
            ll = interpolate([p[0] for p in latlng], seg_start, dist_data)
            ln = interpolate([p[1] for p in latlng], seg_start, dist_data)
            if ll is not None:
                start_lat = round(ll, 6)
                start_lng = round(ln, 6)

        splits.append({
            "activity_id":        activity_id,
            "start_date_local":   start_date_local,
            "split_num":          split_num,
            "split_start_m":      round(seg_start, 1),
            "split_end_m":        round(seg_end, 1),
            "split_dist_m":       round(seg_dist, 1),
            "split_time_s":       round(seg_time_s, 2) if seg_time_s else None,
            "pace_min_km":        pace_min_km,
            "pace_str":           pace_str,
            "avg_hr_bpm":         avg_hr,
            "avg_cadence_rpm":    avg_cad,
            "avg_speed_mps":      round(avg_vel, 4) if avg_vel else None,
            "elevation_change_m": elev_change,
            "altitude_m":         round(alt_start, 1) if alt_start is not None else None,
            "start_lat":          start_lat,
            "start_lng":          start_lng,
        })

        split_num += 1
        target += SPLIT_INTERVAL

    return splits


# ---------------------------------------------------------------------------
# Processing + IO
# ---------------------------------------------------------------------------

def process_activity(a):
    row = {}
    for field in ACTIVITY_FIELDS:
        val = a.get(field)
        if isinstance(val, list):
            val = ",".join(str(v) for v in val)
        if isinstance(val, bool):
            val = int(val)
        row[field] = val if val is not None else ""

    dist_m   = a.get("distance", 0)
    moving_s = a.get("moving_time", 0)
    row["distance_km"]     = round(dist_m / 1000, 4) if dist_m else ""
    row["moving_time_min"] = round(moving_s / 60, 2) if moving_s else ""

    if dist_m and moving_s and moving_s > 0:
        speed = dist_m / moving_s
        if speed > 0:
            p = 1000 / speed / 60
            row["pace_min_km"] = round(p, 4)
            row["pace_str"]    = f"{int(p)}:{int((p % 1) * 60):02d}"
        else:
            row["pace_min_km"] = row["pace_str"] = ""
    else:
        row["pace_min_km"] = row["pace_str"] = ""

    return row


def load_existing_ids(filepath, id_field="id"):
    if not Path(filepath).exists():
        return set()
    with open(filepath, newline="", encoding="utf-8") as f:
        return {row[id_field] for row in csv.DictReader(f) if row.get(id_field)}


def append_rows(rows, filepath):
    if not rows:
        return 0
    file_exists = Path(filepath).exists()
    with open(filepath, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def load_state():
    if not Path(STATE_FILE).exists():
        return {}
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all",  action="store_true", help="Re-fetch all activities")
    parser.add_argument("--days", type=int,            help="Fetch last N days")
    args = parser.parse_args()

    tokens       = load_credentials()
    access_token = get_access_token(tokens)

    # Time window
    after_epoch = None
    if args.all:
        print("\nMode: full re-fetch")
    elif args.days:
        after_epoch = int(time.time()) - (args.days * 86400)
        print(f"\nMode: last {args.days} days")
    else:
        state = load_state()
        last_fetch = state.get("last_fetch_epoch")
        if last_fetch:
            dt = datetime.fromtimestamp(last_fetch)
            print(f"\nMode: incremental since {dt.strftime('%Y-%m-%d %H:%M')}")
            after_epoch = last_fetch
        else:
            print("\nMode: first run — fetching everything")

    # 1. Activity list
    print("\n[1/3] Fetching activity list...")
    activities = fetch_activity_list(access_token, after_epoch)
    print(f"  {len(activities)} activities retrieved.")

    if not activities:
        print("No new activities found.")
        save_state({"last_fetch_epoch": int(time.time())})
        return

    # 2. Activity summaries
    print("\n[2/3] Writing activity summaries...")
    existing_act_ids = load_existing_ids(ACTIVITIES_FILE, "id")
    act_rows     = [process_activity(a) for a in activities]
    new_act_rows = [r for r in act_rows if str(r["id"]) not in existing_act_ids]
    n_act = append_rows(new_act_rows, ACTIVITIES_FILE)
    print(f"  ✓ {n_act} new activities written to {ACTIVITIES_FILE}")

    # 3. Streams + 100m splits
    print(f"\n[3/3] Fetching GPS streams + building 100m splits...")
    existing_split_ids = load_existing_ids(SPLITS_FILE, "activity_id")
    all_splits = []
    skipped = no_stream = 0

    for i, a in enumerate(activities):
        act_id   = str(a["id"])
        act_type = a.get("type", "")
        name     = a.get("name", "")

        if act_id in existing_split_ids:
            skipped += 1
            continue

        if a.get("manual") or a.get("trainer"):
            print(f"  [{i+1}/{len(activities)}] Skipping indoor/manual: {name}")
            no_stream += 1
            continue

        print(f"  [{i+1}/{len(activities)}] {a.get('start_date_local','')[:10]} {act_type}: {name}")
        streams = fetch_streams(act_id, access_token)

        if not streams or "distance" not in streams:
            print(f"    No stream data.")
            no_stream += 1
            continue

        splits = build_100m_splits(act_id, a.get("start_date_local", ""), streams)
        if splits:
            all_splits.extend(splits)
            print(f"    → {len(splits)} splits ({splits[-1]['split_end_m']:.0f}m)")
        else:
            print(f"    → No splits generated.")

    n_splits = append_rows(all_splits, SPLITS_FILE)
    print(f"\n  ✓ {n_splits} split rows written to {SPLITS_FILE}")
    if skipped:   print(f"  ({skipped} already processed, skipped)")
    if no_stream: print(f"  ({no_stream} had no GPS stream)")

    save_state({"last_fetch_epoch": int(time.time())})
    print(f"\nDone.\n  {ACTIVITIES_FILE}\n  {SPLITS_FILE}")


if __name__ == "__main__":
    main()
