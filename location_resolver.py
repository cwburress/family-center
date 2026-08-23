import os
import sys

# Auto-switch to project virtual environment Python if invoked via system python
_script_dir = os.path.dirname(os.path.abspath(__file__))
_venv_python = os.path.join(_script_dir, 'venv', 'bin', 'python3')
if os.path.exists(_venv_python) and sys.executable != _venv_python:
    os.execv(_venv_python, [_venv_python] + sys.argv)

import re
import time
import json
import logging
import threading
import requests
from urllib.parse import quote
from datetime import datetime, timezone
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] (LocationResolver) %(message)s'
)
logger = logging.getLogger("LocationResolver")

# Anchor all file paths to this script's directory so they work under any working directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(SCRIPT_DIR, 'token.json')

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=os.path.join(SCRIPT_DIR, '.env'))
except ImportError:
    env_file = os.path.join(SCRIPT_DIR, '.env')
    if os.path.exists(env_file):
        with open(env_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    os.environ.setdefault(k.strip(), v.strip())

SCOPES = ['https://www.googleapis.com/auth/calendar.events']
CALENDAR_ID = os.getenv('FAMILY_CALENDAR_ID', 'primary')
TIMEZONE = os.getenv('TIMEZONE', 'America/Chicago')
CAL_POLL_MINUTES = int(os.getenv('CALENDAR_POLL_MINUTES', 5))
GOOGLE_PLACES_API_KEY = os.getenv('GOOGLE_PLACES_API_KEY')
MARKER = "📍 "

# Known High Schools & Venues (River Valley / NWA Opponent Schools)
KNOWN_VENUES = {
    r'\brussellville\b': ("Cyclone Stadium (Russellville High School)", "2203 S Knoxville Ave, Russellville, AR 72802"),
    r'\bgreenwood\b': ("Smith-Robinson Stadium (Greenwood High School)", "400 E Gary St, Greenwood, AR 72936"),
    r'\bpea ridge\b': ("Blackhawk Stadium (Pea Ridge High School)", "781 W Pickens Rd, Pea Ridge, AR 72751"),
    r'\bdardanelle\b': ("Bob Keenan Field (Dardanelle High School)", "1079 Hwy 28 W, Dardanelle, AR 72834"),
    r'\bmorrilton\b': ("Devil Dog Stadium (Morrilton High School)", "701 E Harding St, Morrilton, AR 72110"),
    r'\bclarksville\b': ("Clarksville High School", "1701 Clark Rd, Clarksville, AR 72830"),
    r'\bfarmington\b': ("Farmington High School", "12329 N Rhea Mill Rd, Farmington, AR 72730"),
    r'\bharrison\b': ("F.S. Garrison Stadium (Harrison High School)", "925 S Stadium Dr, Harrison, AR 72601"),
    r'\bvilonia\b': ("Phillip Weaver Stadium (Vilonia High School)", "1140 Main St, Vilonia, AR 72173")
}

def clean_title(title):
    """Strip lead marker if present for logic checks."""
    if title.startswith(MARKER):
        return title[len(MARKER):].strip()
    return title.strip()

GENERIC_WORDS = {
    'church', 'dentist', 'ortho', 'doctor', 'birthday', 'party', 'dinner', 
    'lunch', 'breakfast', 'meeting', 'appt', 'appointment', 'practice', 
    'game', 'buddies', 'shopping', 'workout', 'gym', 'school', 'first day',
    'state dance', 'dance'
}

def geocode_location(query):
    q_low = query.strip().lower()
    if not query or len(query.strip()) <= 3 or q_low in GENERIC_WORDS or any(w in q_low for w in ['birthday', 'buddies', 'first day of school', 'workout', 'house', 'home']):
        return None

    search_query = f"{query.strip()}, Arkansas"

    # 1. Try Google Places API if API key is provided
    if GOOGLE_PLACES_API_KEY:
        try:
            # Try Places API (Legacy Text Search)
            url = f"https://maps.googleapis.com/maps/api/place/textsearch/json?query={quote(search_query)}&key={GOOGLE_PLACES_API_KEY}"
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('status') == 'OK' and data.get('results'):
                    place = data['results'][0]
                    name = place.get('name', '')
                    address = place.get('formatted_address', '')
                    if address:
                        clean_addr = address.replace(', USA', '').replace(', United States', '').strip()
                        if name and name.lower() not in clean_addr.lower():
                            return f"{name}, {clean_addr}"
                        return clean_addr

            # Try Places API (New REST endpoint)
            new_url = "https://places.googleapis.com/v1/places:searchText"
            headers = {
                'Content-Type': 'application/json',
                'X-Goog-Api-Key': GOOGLE_PLACES_API_KEY,
                'X-Goog-FieldMask': 'places.displayName,places.formattedAddress'
            }
            resp_new = requests.post(new_url, headers=headers, json={'textQuery': search_query}, timeout=5)
            if resp_new.status_code == 200:
                data_new = resp_new.json()
                places = data_new.get('places', [])
                if places:
                    p = places[0]
                    name = p.get('displayName', {}).get('text', '')
                    address = p.get('formattedAddress', '')
                    if address:
                        clean_addr = address.replace(', USA', '').replace(', United States', '').strip()
                        if name and name.lower() not in clean_addr.lower():
                            return f"{name}, {clean_addr}"
                        return clean_addr
        except Exception as e:
            logger.warning(f"Google Places API lookup error for '{query}': {e}")

    # 2. OpenStreetMap Nominatim Fallback
    try:
        url = f"https://nominatim.openstreetmap.org/search?q={quote(search_query)}&format=json&addressdetails=1&limit=1"
        headers = {'User-Agent': 'FamilyCenterLocationResolver/1.0 (alma-family-center)'}
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if data:
                res = data[0]
                addr = res.get('address', {})
                road = addr.get('road', '')
                house_num = addr.get('house_number', '')
                city = addr.get('city') or addr.get('town') or addr.get('village') or addr.get('county', '')
                state = addr.get('state', 'AR')
                postcode = addr.get('postcode', '')
                
                # Must have a valid street road or house number to be considered a real venue address
                if road and city:
                    street = f"{house_num} {road}".strip()
                    return f"{query}, {street}, {city}, {state} {postcode}".strip(', ')
    except Exception as e:
        logger.warning(f"Nominatim geocoding lookup error for '{query}': {e}")

    return None

def deduce_location(title, description=""):
    """Deduce venue name and full street address based on title and description."""
    clean_t = clean_title(title)
    text_to_search = f"{clean_t} {description}".strip()

    # 1. Handle "@ Opponent" or "at Venue" (Away Games / Places)
    # Use \b word boundary so 'at' doesn't match inside words like 'State', 'Watch', etc.
    at_match = re.search(r'(?:@|\bat\b)\s*([A-Za-z0-9\s\'\-]+)', clean_t, re.IGNORECASE)
    if at_match:
        target = at_match.group(1).strip()
        target_clean = re.sub(r'\(.*?\)', '', target).strip()
        for pattern, (venue_name, address) in KNOWN_VENUES.items():
            if re.search(pattern, target_clean, re.IGNORECASE):
                return f"{venue_name}, {address}"
        geo_res = geocode_location(target_clean)
        if geo_res:
            return geo_res

    # 2. Handle "vs. Opponent" / "vs Opponent" (Home Games)
    vs_match = re.search(r'\bvs\.?\s*([A-Za-z0-9\s\'\-]+)', clean_t, re.IGNORECASE)
    if vs_match:
        if 'away' in description.lower():
            target = vs_match.group(1).strip()
            target_clean = re.sub(r'\(.*?\)', '', target).strip()
            for pattern, (venue_name, address) in KNOWN_VENUES.items():
                if re.search(pattern, target_clean, re.IGNORECASE):
                    return f"{venue_name}, {address}"
        else:
            return "Airedale Stadium (Alma High School), 101 E Main St, Alma, AR 72921"

    # 3. Direct pattern match against KNOWN_VENUES
    for pattern, (venue_name, address) in KNOWN_VENUES.items():
        if re.search(pattern, text_to_search, re.IGNORECASE):
            return f"{venue_name}, {address}"

    # 4. Geocode fallback (Google Places API handles any business/place query)
    if GOOGLE_PLACES_API_KEY:
        return geocode_location(clean_t)
    elif any(k in clean_t.lower() for k in ['stadium', 'high school', 'middle school', 'elementary', 'center', 'hospital', 'clinic', 'walmart', 'target', 'kroger', 'harps', 'park', 'library', 'chaffee', 'academy', 'field', 'gym', 'arena', 'hall', 'church']):
        return geocode_location(clean_t)

    return None

def process_calendar_events(dry_run=False, service=None, on_update_callback=None):
    """Scan upcoming events on Google Calendar and add deduced locations & title markers."""
    if not service and not dry_run:
        if not os.path.exists(TOKEN_PATH):
            logger.error(f"token.json missing at '{TOKEN_PATH}'. Cannot connect to Google Calendar API.")
            return
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        service = build('calendar', 'v3', credentials=creds)

    logger.info(f"Scanning calendar events for CALENDAR_ID='{CALENDAR_ID}' (dry_run={dry_run})...")

    # Time window: Now to 90 days in future
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    
    if dry_run and not service:
        # Load local schedule data for testing if offline
        from add_football_schedule import GAMES
        logger.info("Running dry-run evaluation on sample games list...")
        for g in GAMES:
            title = g['summary']
            deduced = deduce_location(title)
            logger.info(f"Sample: '{title}' -> Deduced Location: '{deduced}'")
        return

    events_result = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=now,
        maxResults=50,
        singleEvents=True,
        orderBy='startTime'
    ).execute()

    events = events_result.get('items', [])
    logger.info(f"Retrieved {len(events)} upcoming events to inspect.")

    updated_count = 0

    for event in events:
        event_id = event['id']
        summary = event.get('summary', '')
        current_location = event.get('location', '').strip()
        description = event.get('description', '')

        # Check if already processed (has marker)
        has_marker = summary.startswith(MARKER)
        
        # If already has marker AND has location, skip
        if has_marker and current_location:
            continue

        new_location = current_location
        new_summary = summary if has_marker else f"{MARKER}{summary}"

        # Deduce location if missing
        if not current_location:
            deduced = deduce_location(clean_title(summary), description)
            if deduced:
                new_location = deduced

        # Prepend 📍 marker ONLY if a location is populated (pre-existing or newly deduced)
        if new_location:
            new_summary = f"{MARKER}{clean_title(summary)}"
        else:
            new_summary = clean_title(summary)

        # If location changed or title marker needs adding
        if (new_location != current_location and new_location) or (new_summary != summary):
            patch_body = {}
            if new_location != current_location and new_location:
                patch_body['location'] = new_location
            if new_summary != summary:
                patch_body['summary'] = new_summary

            if patch_body:
                logger.info(f"Updating Event '{summary}' ({event_id}): {patch_body}")
                if not dry_run:
                    service.events().patch(calendarId=CALENDAR_ID, eventId=event_id, body=patch_body).execute()
                    updated_count += 1
                else:
                    logger.info(f"[DRY RUN] Would patch event {event_id} with {patch_body}")

    logger.info(f"Completed location resolver sweep. Updated {updated_count} events.")

    if updated_count > 0 and on_update_callback:
        try:
            logger.info("Triggering app cache refresh callback...")
            on_update_callback()
        except Exception as e:
            logger.warning(f"Error in on_update_callback: {e}")

DAEMON_LOCK_PATH = os.path.join(SCRIPT_DIR, '.location_resolver.lock')

def is_location_resolver_enabled():
    try:
        settings_path = os.path.join(SCRIPT_DIR, 'bg_settings.json')
        if os.path.exists(settings_path):
            with open(settings_path, 'r') as f:
                settings = json.load(f)
                return settings.get('enable_location_resolver', True)
    except Exception as e:
        logger.warning(f"Error checking location resolver settings: {e}")
    return os.getenv('AUTO_LOCATION_ENABLED', 'true').lower() in ('true', '1', 'yes')

def start_location_resolver_daemon(offset_seconds=60, on_update_callback=None):
    """Start background loop. Uses a filesystem lock so only ONE Gunicorn worker runs the daemon."""
    import fcntl
    try:
        lock_file = open(DAEMON_LOCK_PATH, 'w')
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # We got the lock — this worker owns the daemon
        lock_file.write(str(os.getpid()))
        lock_file.flush()
    except (IOError, OSError):
        logger.info(f"Location Resolver daemon already running in another worker — skipping.")
        return None

    def daemon_loop():
        poll_interval = CAL_POLL_MINUTES * 60
        logger.info(f"Daemon started (PID {os.getpid()}). Sweeping every {CAL_POLL_MINUTES} minute(s), offset by {offset_seconds}s.")
        
        # Initial offset wait
        time.sleep(offset_seconds)
        
        while True:
            try:
                if is_location_resolver_enabled():
                    process_calendar_events(on_update_callback=on_update_callback)
                else:
                    logger.info("Location Resolver is disabled in settings — skipping sweep.")
            except Exception as e:
                logger.error(f"Error during daemon sweep: {e}")
            
            time.sleep(poll_interval)

    thread = threading.Thread(target=daemon_loop, daemon=True, name="LocationResolverThread")
    thread.start()
    return thread

if __name__ == '__main__':
    dry_run = "--dry-run" in sys.argv
    run_once = "--run-once" in sys.argv or dry_run
    
    if run_once:
        process_calendar_events(dry_run=dry_run)
    else:
        logger.info("Starting Location Resolver standalone daemon...")
        t = start_location_resolver_daemon(offset_seconds=1)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Daemon stopped by user.")
