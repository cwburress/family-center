"""
sync_basketball.py - Sync Subscribed Basketball schedules into Google Family Calendar.

Transforms league event titles into:
  🏀 <Daughter>: Alma vs <Opponent>
Examples:
  🏀 Kiki: Alma vs Farmington
  🏀 Claire: Alma vs Bentonville

Preserves game locations (Centerton, Fayetteville, etc.) and court details.
Uses extendedProperties and description tags to prevent duplicate events
and keep updates in sync automatically.
Performs two-way reconciliation:
  - Deletes duplicate sync entries if any occur.
  - Automatically deletes / purges orphaned games when league changes game times or cancels games.
"""

import os
import re
import sys
import json
import argparse
import datetime
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from dotenv import load_dotenv

if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'))

SCOPES = [
    'https://www.googleapis.com/auth/calendar.readonly',
    'https://www.googleapis.com/auth/calendar.events'
]

# Destination calendar: default to FAMILY_CALENDAR_ID
DEFAULT_DEST_CALENDAR = os.getenv('FAMILY_CALENDAR_ID', 'primary')
CONFIG_FILE = os.path.join(BASE_DIR, 'basketball_config.json')

def load_basketball_teams():
    """
    Loads team mapping configuration from basketball_config.json or environment.
    Keeps PII (names, personal calendar IDs) externalized from version control.
    """
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception as e:
            print(f"[WARN] Failed to load {CONFIG_FILE}: {e}")

    env_config = os.getenv('BASKETBALL_SYNC_CONFIG')
    if env_config:
        try:
            data = json.loads(env_config)
            if isinstance(data, list):
                return data
        except Exception as e:
            print(f"[WARN] Failed to parse BASKETBALL_SYNC_CONFIG: {e}")

    return []

def get_calendar_service():
    token_file = os.path.join(BASE_DIR, 'token.json')
    if not os.path.exists(token_file):
        raise FileNotFoundError(f"token.json not found at {token_file}. Run get_token.py first.")
    
    creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # Save refreshed token
            with open(token_file, 'w') as f:
                f.write(creds.to_json())
        else:
            raise PermissionError("Credentials invalid or expired. Re-run get_token.py.")
    return build('calendar', 'v3', credentials=creds)

def clean_opponent(opp_str):
    """
    Strips coach suffix if present.
    'Farmington - Brown' -> 'Farmington'
    'Bentonville West - Rooney' -> 'Bentonville West'
    """
    opp = opp_str.strip()
    if ' - ' in opp:
        parts = opp.split(' - ')
        return parts[0].strip()
    return opp

def transform_title(daughter, team_pattern, our_team_display, raw_summary):
    """
    Transforms raw summary into '🏀 <Daughter>: Alma vs <Opponent>'
    """
    raw = (raw_summary or '').strip()
    vs_match = re.split(r'\s+vs\.?\s+', raw, flags=re.IGNORECASE)
    
    if len(vs_match) == 2:
        part_a, part_b = vs_match[0].strip(), vs_match[1].strip()
        if re.search(team_pattern, part_a, flags=re.IGNORECASE):
            opponent = clean_opponent(part_b)
            return f"🏀 {daughter}: {our_team_display} vs {opponent}"
        elif re.search(team_pattern, part_b, flags=re.IGNORECASE):
            opponent = clean_opponent(part_a)
            return f"🏀 {daughter}: {our_team_display} vs {opponent}"
        else:
            return f"🏀 {daughter}: {clean_opponent(part_a)} vs {clean_opponent(part_b)}"
    
    at_match = re.split(r'\s+@\s+', raw)
    if len(at_match) == 2:
        part_a, part_b = at_match[0].strip(), at_match[1].strip()
        if re.search(team_pattern, part_a, flags=re.IGNORECASE):
            opponent = clean_opponent(part_b)
            return f"🏀 {daughter}: {our_team_display} vs {opponent}"
        elif re.search(team_pattern, part_b, flags=re.IGNORECASE):
            opponent = clean_opponent(part_a)
            return f"🏀 {daughter}: {our_team_display} vs {opponent}"

    cleaned = re.sub(team_pattern, our_team_display, raw, flags=re.IGNORECASE).strip()
    return f"🏀 {daughter}: {cleaned}"

def build_description(daughter, team_config, source_event):
    raw_desc = (source_event.get('description') or '').strip()
    summary = source_event.get('summary', '')
    source_id = source_event.get('id', '')
    cal_id = team_config['calendar_id']

    # Extract court or league info if available
    court_info = ""
    for line in raw_desc.splitlines():
        if "Court" in line or "Lindsey" in line or "Gatorade" in line or "AAO" in line:
            court_info = line.strip()
            break
    if not court_info and raw_desc:
        court_info = raw_desc.split('(')[0].strip()

    desc_lines = [
        f"🏀 Girls Basketball Game: {daughter}",
        f"Team: {team_config.get('team_display', 'Alma')} ({daughter})",
        f"Original Matchup: {summary}",
    ]
    if court_info:
        desc_lines.append(f"Details: {court_info}")
    
    desc_lines.append("")
    desc_lines.append(f"<!-- sync_source: girls_basketball_sync | source_cal: {cal_id} | source_id: {source_id} -->")
    return "\n".join(desc_lines)

def normalize_time(val):
    if not val:
        return ""
    try:
        if 'T' in val:
            dt = datetime.datetime.fromisoformat(val.replace('Z', '+00:00'))
            return dt.astimezone(datetime.timezone.utc).isoformat().replace('+00:00', 'Z')
        return val
    except Exception:
        return val

def is_game_placeholder(summary, daughter):
    s = summary.lower()
    d = daughter.lower()
    if d in s and any(k in s for k in ['basketball', 'bball', 'game', 'hoops']):
        if any(k in s for k in ['practice', 'workout', 'training', 'camp']):
            return False
        if '🏀' in s:
            return False
        return True
    return False

def sync_basketball_games(dry_run=False, days_ahead=90, dest_calendar_id=None, adopt_placeholders=True):
    """
    Syncs subscribed basketball schedules into Google Family Calendar.
    Performs full two-way reconciliation:
      - Inserts new games
      - Updates changed games
      - Deletes duplicates
      - Deletes orphaned / moved games
    Returns summary statistics.
    """
    teams = load_basketball_teams()
    if not teams:
        print("[Basketball Sync] No teams configured in basketball_config.json or environment.")
        return {"created": [], "updated": [], "deleted": [], "skipped": [], "errors": ["No teams configured"]}

    service = get_calendar_service()
    dest_cal = dest_calendar_id or DEFAULT_DEST_CALENDAR
    
    now = datetime.datetime.now(datetime.timezone.utc)
    # Start looking from 7 days ago to catch any recent games / modifications
    time_min = (now - datetime.timedelta(days=7)).isoformat().replace('+00:00', 'Z')
    time_max = (now + datetime.timedelta(days=days_ahead)).isoformat().replace('+00:00', 'Z')

    print(f"[Basketball Sync] Checking Family Calendar: {dest_cal}")
    print(f"[Basketball Sync] Window: {time_min} to {time_max} (dry_run={dry_run}, adopt_placeholders={adopt_placeholders})")

    results = {
        "created": [],
        "updated": [],
        "deleted": [],
        "skipped": [],
        "errors": []
    }

    # STEP 1: Fetch active events from all source calendars
    source_events_by_cal = {}
    all_active_source_ids = set()

    for team in teams:
        cal_id = team.get('calendar_id')
        daughter = team.get('daughter', '')
        if not cal_id or not daughter:
            continue
        try:
            src_res = service.events().list(
                calendarId=cal_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            items = src_res.get('items', [])
            source_events_by_cal[cal_id] = items
            for item in items:
                all_active_source_ids.add(item['id'])
            print(f"[Basketball Sync] Found {len(items)} active league events for {daughter}.")
        except Exception as e:
            err = f"Failed to fetch source calendar {cal_id}: {e}"
            print(f"[ERROR] {err}")
            results['errors'].append(err)

    # STEP 2: Fetch all events from destination (Family) calendar in window
    dest_events = []
    page_token = None
    while True:
        res = service.events().list(
            calendarId=dest_cal,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            pageToken=page_token
        ).execute()
        dest_events.extend(res.get('items', []))
        page_token = res.get('nextPageToken')
        if not page_token:
            break

    # STEP 3: Identify duplicates and orphaned/moved games on Family Calendar
    synced_by_source_id = {}
    synced_by_norm_time_title = {}
    placeholders_by_norm_time = {}

    for dev in dest_events:
        dev_id = dev.get('id')
        dev_summary = (dev.get('summary') or '').strip()
        raw_start = dev.get('start', {}).get('dateTime') or dev.get('start', {}).get('date')
        norm_start = normalize_time(raw_start)
        dev_desc = dev.get('description') or ''
        dev_props = dev.get('extendedProperties', {}).get('private', {})

        is_our_sync = (
            dev_props.get('sync_source') == 'girls_basketball_sync' or
            'sync_source: girls_basketball_sync' in dev_desc
        )

        source_id = dev_props.get('source_event_id')
        source_cal = dev_props.get('source_cal_id')
        if not source_id and 'source_id:' in dev_desc:
            m = re.search(r'source_id:\s*([^\s-]+(?:_[^\s-]+)?)', dev_desc)
            if m:
                source_id = m.group(1).strip()
        if not source_cal and 'source_cal:' in dev_desc:
            m = re.search(r'source_cal:\s*([^\s|]+)', dev_desc)
            if m:
                source_cal = m.group(1).strip()

        if is_our_sync:
            # 3a. Check for duplicate of the same source_id
            if source_id and source_id in synced_by_source_id:
                print(f"  [DELETE DUPLICATE] Removing duplicate event '{dev_summary}' ({dev_id}) for source_id '{source_id}'")
                if not dry_run:
                    try:
                        service.events().delete(calendarId=dest_cal, eventId=dev_id).execute()
                        results['deleted'].append({'id': dev_id, 'title': dev_summary, 'reason': 'Duplicate'})
                    except Exception as del_err:
                        print(f"  [ERROR] Failed to delete duplicate {dev_id}: {del_err}")
                        results['errors'].append(str(del_err))
                else:
                    results['deleted'].append({'id': dev_id, 'title': dev_summary, 'reason': 'Duplicate', 'dry_run': True})
                continue

            # 3b. Check for orphaned/moved game (source_id no longer in active source calendar)
            if source_cal in source_events_by_cal:
                if source_id and source_id not in all_active_source_ids:
                    print(f"  [DELETE ORPHAN] Removing cancelled/moved game '{dev_summary}' ({dev_id}) at {raw_start}")
                    if not dry_run:
                        try:
                            service.events().delete(calendarId=dest_cal, eventId=dev_id).execute()
                            results['deleted'].append({'id': dev_id, 'title': dev_summary, 'reason': 'Game moved or cancelled'})
                        except Exception as del_err:
                            print(f"  [ERROR] Failed to delete orphan {dev_id}: {del_err}")
                            results['errors'].append(str(del_err))
                    else:
                        results['deleted'].append({'id': dev_id, 'title': dev_summary, 'reason': 'Game moved or cancelled', 'dry_run': True})
                    continue

            # Event is valid and kept
            if source_id:
                synced_by_source_id[source_id] = dev

        # Non-duplicate index lookups
        clean_summary = dev_summary.replace('📍', '').strip()
        if norm_start and clean_summary:
            synced_by_norm_time_title[(norm_start, clean_summary.lower())] = dev

        if norm_start and dev_summary:
            placeholders_by_norm_time.setdefault(norm_start, []).append(dev)

    # STEP 4: Process active source events (insert or update)
    for team in teams:
        cal_id = team.get('calendar_id')
        daughter = team.get('daughter', '')
        team_pattern = team.get('team_pattern', '')
        team_display = team.get('team_display', 'Alma')

        src_items = source_events_by_cal.get(cal_id, [])

        for sev in src_items:
            sev_id = sev.get('id')
            raw_summary = sev.get('summary', '')
            start = sev.get('start', {})
            end = sev.get('end', {})
            location = (sev.get('location') or '').strip()
            raw_start = start.get('dateTime') or start.get('date')
            norm_start = normalize_time(raw_start)

            new_title = transform_title(daughter, team_pattern, team_display, raw_summary)
            new_desc = build_description(daughter, team, sev)

            # 1. Match by source event ID
            existing = synced_by_source_id.get(sev_id)

            # 2. Match by exact normalized start time + clean title
            if not existing and norm_start:
                clean_title = new_title.replace('📍', '').strip().lower()
                existing = synced_by_norm_time_title.get((norm_start, clean_title))

            # 3. Match placeholder at same start time if adopt_placeholders is True
            adopted_placeholder = False
            if not existing and adopt_placeholders and norm_start in placeholders_by_norm_time:
                for cand in placeholders_by_norm_time[norm_start]:
                    c_sum = cand.get('summary', '')
                    if is_game_placeholder(c_sum, daughter):
                        existing = cand
                        adopted_placeholder = True
                        print(f"  [ADOPTING PLACEHOLDER] Found manual placeholder '{c_sum}' at {raw_start} -> Upgrading to official game.")
                        break

            event_body = {
                'summary': new_title,
                'location': location,
                'description': new_desc,
                'start': start,
                'end': end,
                'extendedProperties': {
                    'private': {
                        'sync_source': 'girls_basketball_sync',
                        'source_cal_id': cal_id,
                        'source_event_id': sev_id,
                        'daughter': daughter
                    }
                }
            }

            if existing:
                ex_summary = existing.get('summary', '')
                clean_ex_summary = ex_summary.replace('📍', '').strip()
                clean_new_title = new_title.replace('📍', '').strip()

                ex_loc = (existing.get('location') or '').strip()
                ex_start = normalize_time(existing.get('start', {}).get('dateTime') or existing.get('start', {}).get('date'))
                ex_end = normalize_time(existing.get('end', {}).get('dateTime') or existing.get('end', {}).get('date'))

                cur_start = normalize_time(start.get('dateTime') or start.get('date'))
                cur_end = normalize_time(end.get('dateTime') or end.get('date'))

                needs_update = (
                    adopted_placeholder or
                    clean_ex_summary != clean_new_title or
                    ex_loc != location or
                    ex_start != cur_start or
                    ex_end != cur_end
                )

                if needs_update:
                    action_type = "ADOPT & UPGRADE" if adopted_placeholder else "UPDATE"
                    print(f"  [{action_type}] {daughter}: '{ex_summary}' -> '{new_title}' at {raw_start} (loc: {location})")
                    if not dry_run:
                        try:
                            service.events().patch(
                                calendarId=dest_cal,
                                eventId=existing['id'],
                                body=event_body
                            ).execute()
                            results['updated'].append({
                                'daughter': daughter,
                                'id': existing['id'],
                                'title': new_title,
                                'start': raw_start,
                                'adopted_placeholder': adopted_placeholder
                            })
                            print(f"  -> Successfully updated {existing['id']}")
                        except Exception as patch_err:
                            err_msg = f"Failed to update {existing['id']}: {patch_err}"
                            print(f"  [ERROR] {err_msg}")
                            results['errors'].append(err_msg)
                    else:
                        results['updated'].append({
                            'daughter': daughter,
                            'id': existing['id'],
                            'title': new_title,
                            'start': raw_start,
                            'dry_run': True,
                            'adopted_placeholder': adopted_placeholder
                        })
                else:
                    results['skipped'].append({
                        'daughter': daughter,
                        'title': new_title,
                        'start': raw_start,
                        'reason': 'Up to date'
                    })
                    print(f"  [OK] {new_title} at {raw_start} already up to date.")
            else:
                # Needs creation
                print(f"  [NEW EVENT] {daughter}: '{new_title}' at {raw_start} (loc: {location})")
                if not dry_run:
                    try:
                        created = service.events().insert(
                            calendarId=dest_cal,
                            body=event_body
                        ).execute()
                        created_id = created.get('id')
                        # Register in lookup
                        synced_by_source_id[sev_id] = created
                        if norm_start:
                            clean_title = new_title.replace('📍', '').strip().lower()
                            synced_by_norm_time_title[(norm_start, clean_title)] = created

                        results['created'].append({
                            'daughter': daughter,
                            'id': created_id,
                            'title': new_title,
                            'start': raw_start
                        })
                        print(f"  -> Created event {created_id}: {new_title}")
                    except Exception as ins_err:
                        err_msg = f"Failed to insert '{new_title}': {ins_err}"
                        print(f"  [ERROR] {err_msg}")
                        results['errors'].append(err_msg)
                else:
                    results['created'].append({
                        'daughter': daughter,
                        'title': new_title,
                        'start': raw_start,
                        'dry_run': True
                    })

    print(f"\n[Basketball Sync Summary]")
    print(f"  Created: {len(results['created'])}")
    print(f"  Updated: {len(results['updated'])}")
    print(f"  Deleted: {len(results['deleted'])}")
    print(f"  Skipped: {len(results['skipped'])}")
    print(f"  Errors:  {len(results['errors'])}")
    return results

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Sync Basketball schedules into Family Calendar")
    parser.add_argument('--dry-run', action='store_true', help="Preview changes without writing to Google Calendar")
    parser.add_argument('--days', type=int, default=90, help="Days ahead to sync (default: 90)")
    parser.add_argument('--calendar-id', type=str, default=None, help="Destination calendar ID")
    parser.add_argument('--no-adopt', action='store_false', dest='adopt_placeholders', help="Do not adopt existing manual placeholder entries")
    args = parser.parse_args()

    sync_basketball_games(
        dry_run=args.dry_run,
        days_ahead=args.days,
        dest_calendar_id=args.calendar_id,
        adopt_placeholders=args.adopt_placeholders
    )
