import datetime
import json
import os
import re
import requests
import threading
import time
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, jsonify
from werkzeug.utils import secure_filename
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from dotenv import load_dotenv

# Load variables from .env
load_dotenv()

app = Flask(__name__)

# Force absolute paths based strictly on where app.py physically lives
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.config['UPLOAD_FOLDER'] = os.path.join(BASE_DIR, 'static', 'backgrounds')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
SETTINGS_FILE = os.path.join(BASE_DIR, 'bg_settings.json')
ACTIVITY_FILE = os.path.join(BASE_DIR, 'activity.json')
MENU_FILE = os.path.join(BASE_DIR, 'menu.json')
ENTREE_IMG_FILE = os.path.join(BASE_DIR, 'entree_images.json')
ENTREE_IMG_FOLDER = os.path.join(BASE_DIR, 'static', 'entrees')
os.makedirs(ENTREE_IMG_FOLDER, exist_ok=True)

def slugify_entree(name):
    return re.sub(r'[^a-z0-9]+', '-', str(name).lower()).strip('-')

def load_entree_images():
    mapping = {}
    if os.path.exists(ENTREE_IMG_FILE):
        try:
            with open(ENTREE_IMG_FILE, 'r') as f:
                mapping = json.load(f)
        except Exception as e:
            print(f"Error loading entree images: {e}")
    return mapping

def save_entree_images(mapping):
    with open(ENTREE_IMG_FILE, 'w') as f:
        json.dump(mapping, f, indent=2)

def load_bg_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                settings = json.load(f)
                # Ensure new keys exist for older configs
                if "enable_radar" not in settings:
                    settings["enable_radar"] = True
                if "enable_location_resolver" not in settings:
                    settings["enable_location_resolver"] = True
                if "enable_clothing" not in settings:
                    settings["enable_clothing"] = True
                if "radar_coords" not in settings:
                    settings["radar_coords"] = f"{HOME_LAT}, {HOME_LON}" if (HOME_LAT != '0.0' or HOME_LON != '0.0') else ""
                if "radar_loop" not in settings:
                    settings["radar_loop"] = True
                if "radar_filter_mode" not in settings:
                    settings["radar_filter_mode"] = "clean"
                if "radar_reset_interval" not in settings:
                    settings["radar_reset_interval"] = 10
                if "schedule_reset_interval" not in settings:
                    settings["schedule_reset_interval"] = 15
                if "panel_opacity" not in settings:
                    settings["panel_opacity"] = 35
                return settings
        except Exception:
            pass
    default_coords = f"{HOME_LAT}, {HOME_LON}" if (HOME_LAT != '0.0' or HOME_LON != '0.0') else ""
    return {"rotate": False, "interval": 300, "selected": ["background.jpg"], "enable_radar": True, "enable_location_resolver": True, "enable_clothing": True, "radar_coords": default_coords, "radar_loop": True, "radar_filter_mode": "clean", "radar_reset_interval": 10, "schedule_reset_interval": 15, "panel_opacity": 35}

def save_bg_settings(new_settings):
    settings = load_bg_settings()
    if isinstance(new_settings, dict):
        settings.update(new_settings)
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(settings, f)

def is_radar_enabled():
    env_enable = os.getenv('ENABLE_RADAR', 'true').lower() in ('true', '1', 'yes')
    if not env_enable:
        return False
    settings = load_bg_settings()
    return settings.get('enable_radar', True)

def is_clothing_enabled():
    settings = load_bg_settings()
    return settings.get('enable_clothing', True)

def get_radar_coordinates():
    settings = load_bg_settings()
    custom_coords = settings.get('radar_coords', '').strip()
    if custom_coords:
        try:
            parts = [p.strip() for p in custom_coords.replace(',', ' ').split() if p.strip()]
            if len(parts) >= 2:
                lat = float(parts[0])
                lon = float(parts[1])
                return lat, lon
        except Exception:
            pass
    try:
        return float(HOME_LAT or 0.0), float(HOME_LON or 0.0)
    except Exception:
        return 0.0, 0.0

SCOPES = [
    'https://www.googleapis.com/auth/calendar.readonly',
    'https://www.googleapis.com/auth/calendar.events'
]
CALENDAR_SETTINGS_FILE = os.path.join(BASE_DIR, 'calendar_settings.json')

def load_calendar_settings():
    if os.path.exists(CALENDAR_SETTINGS_FILE):
        try:
            with open(CALENDAR_SETTINGS_FILE, 'r') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    if "selected_calendar_ids" not in data:
                        data["selected_calendar_ids"] = [CALENDAR_ID] if CALENDAR_ID else ["primary"]
                    return data
        except Exception as e:
            print(f"[WARN] Error loading {CALENDAR_SETTINGS_FILE}: {e}")
    default_ids = [CALENDAR_ID] if CALENDAR_ID else ["primary"]
    return {
        "selected_calendar_ids": default_ids,
        "calendar_metadata": {}
    }

def save_calendar_settings(settings):
    try:
        with open(CALENDAR_SETTINGS_FILE, 'w') as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        print(f"[ERROR] Failed to save {CALENDAR_SETTINGS_FILE}: {e}")
THEME = os.getenv('THEME', 'default')
CALENDAR_ID = os.getenv('FAMILY_CALENDAR_ID', 'primary')
HOME_LAT = os.getenv('HOME_LAT', '0.0')
HOME_LON = os.getenv('HOME_LON', '0.0')
TIMEZONE = os.getenv('TIMEZONE', 'UTC')

# Polling intervals in minutes (defaults fallback to 5, 5, 60, and 5)
CAL_POLL = int(os.getenv('CALENDAR_POLL_MINUTES', 5)) * 60
WX_POLL = int(os.getenv('WEATHER_POLL_MINUTES', 5)) * 60
LUNCH_POLL = int(os.getenv('LUNCH_POLL_MINUTES', 60)) * 60
ACTIVITY_POLL = int(os.getenv('ACTIVITY_POLL_MINUTES', 60)) * 60
UI_REFRESH = int(os.getenv('UI_REFRESH_MINUTES', 5)) * 60000  # milliseconds for JS

# ==========================================
# GOOGLE CALENDAR LOGIC
# ==========================================
def get_events():
    creds = None
    token_file = os.path.join(BASE_DIR, 'token.json')
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    # Auto-refresh the token if it expires
    if not creds or not creds.valid:
        try:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                return [{"summary": "Auth Error: Token missing or invalid", "start": {}}]
        except Exception as e:
            print(f"Token Refresh Error: {e}")
            return [{"summary": "Auth Error: Token expired. Please re-authenticate.", "start": {}}]
 
    try:
        service = build('calendar', 'v3', credentials=creds)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z')
        
        cal_settings = load_calendar_settings()
        selected_ids = cal_settings.get('selected_calendar_ids', [])
        if not selected_ids:
            selected_ids = [CALENDAR_ID] if CALENDAR_ID else ['primary']

        raw_events = []
        seen_event_keys = set()

        for cid in selected_ids:
            try:
                events_result = service.events().list(
                    calendarId=cid,
                    timeMin=now,
                    maxResults=30,
                    singleEvents=True,
                    orderBy='startTime'
                ).execute()
                for ev in events_result.get('items', []):
                    st = ev.get('start', {})
                    start_str = st.get('dateTime') or st.get('date') or ''
                    summary_clean = (ev.get('summary') or '').strip().lower()
                    dedup_key = (start_str, summary_clean)
                    if dedup_key not in seen_event_keys:
                        seen_event_keys.add(dedup_key)
                        ev['_source_calendar_id'] = cid
                        raw_events.append(ev)
            except Exception as cal_err:
                print(f"[WARN] Error fetching events from calendar '{cid}': {cal_err}")

        # Re-sort all combined events chronologically by start time
        def _get_sort_time(ev):
            st = ev.get('start', {})
            return st.get('dateTime') or st.get('date') or ''

        raw_events.sort(key=_get_sort_time)
        future_events = []
        
        # Build the 4-day focus window starting from today
        # Build the single-day focus window for Today
        current_time = datetime.datetime.now()
        today_date = current_time.date()
        today_str = today_date.strftime('%Y-%m-%d')
        
        today_events = []
        
        # Setup the display strings for the large Today header
        today_display = {
            'month': today_date.strftime('%b').upper(),
            'day': str(today_date.day),
            'weekday': today_date.strftime('%A')
        }

        for event in raw_events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            end_raw = event.get('end', {}).get('dateTime', event.get('end', {}).get('date'))
            
            span_days = 1
            if start and end_raw:
                try:
                    if 'T' not in start and 'T' not in end_raw:
                        # All-day event: exclusive end date
                        s_dt = datetime.datetime.fromisoformat(start)
                        e_dt = datetime.datetime.fromisoformat(end_raw)
                        span_days = (e_dt - s_dt).days
                    else:
                        # Timed event: evaluate using local timezone to avoid UTC day-rollover
                        s_dt = datetime.datetime.fromisoformat(start.replace('Z', '+00:00')).astimezone(ZoneInfo(TIMEZONE))
                        e_dt = datetime.datetime.fromisoformat(end_raw.replace('Z', '+00:00')).astimezone(ZoneInfo(TIMEZONE))
                        span_days = (e_dt.date() - s_dt.date()).days + 1
                        
                    if span_days < 1:
                        span_days = 1
                except Exception:
                    span_days = 1
            
            for i in range(span_days):
                try:
                    if 'T' in start:
                        dt_orig = datetime.datetime.fromisoformat(start.replace('Z', '+00:00'))
                        dt_orig = dt_orig.astimezone(ZoneInfo(TIMEZONE))
                        dt = dt_orig + datetime.timedelta(days=i)
                        
                        if span_days > 1:
                            if i == 0:
                                time_str = dt_orig.strftime('%-I:%M%p').lower()
                            elif i == span_days - 1:
                                end_dt = datetime.datetime.fromisoformat(end_raw.replace('Z', '+00:00')).astimezone(ZoneInfo(TIMEZONE))
                                time_str = "Ends " + end_dt.strftime('%-I:%M%p').lower()
                            else:
                                time_str = "Ongoing"
                        else:
                            time_str = dt.strftime('%-I:%M%p').lower()
                    else:
                        dt = datetime.datetime.fromisoformat(start) + datetime.timedelta(days=i)
                        time_str = "All day"
                    
                    full_date = dt.strftime('%Y-%m-%d')
                    
                    # If an ongoing multi-day event started before today, skip the past days
                    if full_date < today_str:
                        continue
                        
                    end_time_str = ""
                    if end_raw and 'T' in end_raw and span_days == 1:
                        end_dt = datetime.datetime.fromisoformat(end_raw.replace('Z', '+00:00'))
                        end_dt = end_dt.astimezone(ZoneInfo(TIMEZONE))
                        end_time_str = end_dt.strftime('%-I:%M%p').lower()
                    
                    day_num = dt.strftime('%-d')
                    month_str = dt.strftime('%b').upper()
                    day_name_full = dt.strftime('%A').upper()
                    is_today = (full_date == today_str)
                    
                except Exception:
                    time_str = start
                    end_time_str = ""
                    day_num = ""
                    month_str = ""
                    day_name_full = ""
                    full_date = ""
                    is_today = False
                    
                event_data = {
                    'summary': event.get('summary', 'Busy'),
                    'time_str': time_str,
                    'end_time_str': end_time_str,
                    'day_num': day_num,
                    'month_str': month_str,
                    'day_name_full': day_name_full,
                    'full_date': full_date,
                    'is_today': is_today,
                    'description': event.get('description', ''),
                    'location': event.get('location', ''),
                    'calendar_id': event.get('_source_calendar_id', '')
                }
                
                # Sort events into Today vs Future Schedule
                if is_today:
                    today_events.append(event_data)
                else:
                    future_events.append(event_data)
                    
        # Since expanding multi-day events appends them sequentially, resort future events by date.
        # Python's sort is stable, so timed events keep their original chronological order.
        future_events.sort(key=lambda x: x['full_date'])
            
        return {'today': today_events, 'future': future_events, 'today_display': today_display}

    except Exception as e:
        return [{"summary": f"API Error: {e}", "start": {}}]


# ==========================================
# WEATHER & CLOTHING LOGIC
# ==========================================
def get_clothing_recommendation(morning_temp, afternoon_temp, will_rain, max_uv=0):
    outfit = []

    if afternoon_temp >= 80:
        outfit.append("Shorts & T-Shirt")
    elif 70 <= afternoon_temp < 80:
        outfit.append("Leggings or Shorts & T-Shirt")
    elif 55 <= afternoon_temp < 70:
        outfit.append("Leggings & T-Shirt")
    elif 40 <= afternoon_temp < 55:
        outfit.append("Leggings & Long Sleeves")
    else:
        outfit.append("Warm Pants & Heavy Sweater")

    if morning_temp < 40:
        outfit.append("Heavy Winter Coat")
        if morning_temp < 32:
            outfit.append("Hat & Gloves")
    elif morning_temp < 55:
        outfit.append("Light Jacket or Hoodie")
    elif morning_temp < 65 and afternoon_temp >= 70:
        outfit.append("Zip-up Hoodie")

    if will_rain:
        outfit.append("Raincoat or Umbrella")

    return outfit


def parse_nws_forecast(hourly_periods):
    morning_temp = None
    afternoon_temp = None
    will_rain = False
    hourly_display = []
    
    today = datetime.date.today()
    
    for period in hourly_periods:
        dt = datetime.datetime.fromisoformat(period['startTime'])
        
        if dt.date() == today:
            formatted_time = dt.strftime('%I %p').lstrip('0')
            pop = period.get('probabilityOfPrecipitation', {}).get('value') or 0
            
            hourly_display.append({
                'time': formatted_time,
                'temp': period['temperature'],
                'pop': pop
            })

            if dt.hour == 6:
                morning_temp = period['temperature']
            elif dt.hour == 13:
                afternoon_temp = period['temperature']
                
            if 6 <= dt.hour <= 15:
                if pop >= 40:
                    will_rain = True
                    
    if morning_temp is None and len(hourly_periods) > 0:
        morning_temp = hourly_periods[0]['temperature']
        
    if afternoon_temp is None and len(hourly_periods) > 0:
        afternoon_temp = hourly_periods[0]['temperature']

    return morning_temp, afternoon_temp, will_rain, hourly_display


def get_active_alerts(headers):
    if HOME_LAT == '0.0' or HOME_LON == '0.0':
        return []
        
    url = f"https://api.weather.gov/alerts/active?point={HOME_LAT},{HOME_LON}"
    alerts = []
    
    try:
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        for feature in data.get('features', []):
            props = feature.get('properties', {})
            alerts.append({
                'event': props.get('event', 'Weather Alert'),
                'severity': props.get('severity', 'Unknown'),
                'headline': props.get('headline', '')
            })
    except Exception as e:
        print(f"Alerts API Error: {e}")
        
    return alerts


def get_radar_sweep_status():
    # Uses IEM Mesonet API to check sweep gaps for the local NWS radar
    radar_id = os.getenv('RADAR_ID', 'SRX') # Default to KSRX (Fort Smith/Alma)
    end_dt = datetime.datetime.now(datetime.timezone.utc)
    start_dt = end_dt - datetime.timedelta(hours=2)
    
    url = f"https://mesonet.agron.iastate.edu/json/radar?operation=list&product=N0Q&radar={radar_id}&start={start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}&end={end_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        
        # Regex pull ISO timestamps regardless of exact JSON schema structure
        ts_strings = re.findall(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", resp.text)
        
        if not ts_strings:
            return None
            
        # Parse, deduplicate, and sort
        timestamps = sorted(list(set([datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ") for ts in ts_strings])))
        
        if len(timestamps) < 4:
            return None
            
        recent_scans = timestamps[-4:]
        gaps = [(recent_scans[i] - recent_scans[i-1]).total_seconds() / 60.0 for i in range(1, 4)]
        
        # If any recent gap is ~6 mins or less, it's in Precipitation mode
        if any(gap <= 6.5 for gap in gaps):
            return True
            
        # Cooldown: only return False if definitively back in Clear Air mode
        if all(gap >= 8.5 for gap in gaps):
            return False
            
        return None # Middle ground, hold previous state or fallback
    except Exception as e:
        print(f"Sweep Check Error: {e}")
        return None


def get_weather():
    grid = os.getenv('NWS_GRID')
    user_agent = os.getenv('NWS_USER_AGENT', 'FamilyCenterDashboard/1.0 (admin@localhost)')
    
    if not grid:
        return None
        
    hourly_url = f"https://api.weather.gov/gridpoints/{grid}/forecast/hourly"
    daily_url = f"https://api.weather.gov/gridpoints/{grid}/forecast"
    headers = {"User-Agent": user_agent}
    
    try:
        # 1. Process Hourly for Clothing and Display
        hourly_resp = requests.get(hourly_url, headers=headers, timeout=5)
        hourly_resp.raise_for_status()
        hourly_periods = hourly_resp.json()['properties']['periods']
        
        morning_temp, afternoon_temp, will_rain, hourly_display = parse_nws_forecast(hourly_periods)
        
        # Grab current temp, feels_like, current humidity, max UV index, daily heat index, and daily humidity from Open-Meteo
        max_uv = 0
        current_temp = None
        feels_like = None
        current_humidity = None
        heat_index_map = {}
        humidity_map = {}
        if HOME_LAT != '0.0' and HOME_LON != '0.0':
            try:
                om_url = f"https://api.open-meteo.com/v1/forecast?latitude={HOME_LAT}&longitude={HOME_LON}&current=temperature_2m,apparent_temperature,relative_humidity_2m&daily=uv_index_max,apparent_temperature_max,relative_humidity_2m_mean&timezone=auto&forecast_days=7&temperature_unit=fahrenheit"
                om_resp = requests.get(om_url, timeout=10)
                if om_resp.status_code == 200:
                    om_data = om_resp.json()
                    max_uv = om_data.get('daily', {}).get('uv_index_max', [0])[0] or 0
                    
                    # Grab the current temp, feels_like, and humidity
                    raw_temp = om_data.get('current', {}).get('temperature_2m')
                    if raw_temp is not None:
                        current_temp = round(raw_temp)
                    
                    raw_feels = om_data.get('current', {}).get('apparent_temperature')
                    if raw_feels is not None:
                        feels_like = round(raw_feels)

                    raw_humidity = om_data.get('current', {}).get('relative_humidity_2m')
                    if raw_humidity is not None:
                        current_humidity = round(raw_humidity)

                    # Build daily Heat Index and Humidity maps (YYYY-MM-DD -> rounded value)
                    om_daily = om_data.get('daily', {})
                    times = om_daily.get('time', [])
                    apparent_maxes = om_daily.get('apparent_temperature_max', [])
                    mean_humidities = om_daily.get('relative_humidity_2m_mean', [])
                    for d_str, app_max in zip(times, apparent_maxes):
                        if app_max is not None:
                            heat_index_map[d_str] = round(app_max)
                    for d_str, mean_hum in zip(times, mean_humidities):
                        if mean_hum is not None:
                            humidity_map[d_str] = round(mean_hum)
            except Exception as e:
                print(f"Open-Meteo API Error: {e}")

        # Fallback for current humidity from NWS hourly if Open-Meteo didn't supply it
        if current_humidity is None and hourly_periods:
            first_h_rh = hourly_periods[0].get('relativeHumidity', {})
            if first_h_rh and first_h_rh.get('value') is not None:
                current_humidity = round(first_h_rh['value'])

        clothing = get_clothing_recommendation(morning_temp, afternoon_temp, will_rain, max_uv)
        
        # 2. Process Daily for the 5-Day UI
        daily_resp = requests.get(daily_url, headers=headers, timeout=5)
        daily_resp.raise_for_status()
        daily_periods_raw = daily_resp.json()['properties']['periods']
        
        daily_forecast = []
        skip_next = False
        
        for i, period in enumerate(daily_periods_raw):
            if skip_next:
                skip_next = False
                continue
                
            if len(daily_forecast) >= 5:
                break
                
            # Skip the leading overnight period if updating early in the morning
            if i == 0 and not period['isDaytime']:
                continue
                
            dt = datetime.datetime.fromisoformat(period['startTime'])
            day_name = dt.strftime('%a')
            date_str = dt.strftime('%Y-%m-%d')
            heat_idx = heat_index_map.get(date_str)
            hum_idx = humidity_map.get(date_str)

            if period['isDaytime'] and i + 1 < len(daily_periods_raw):
                night_period = daily_periods_raw[i+1]
                
                # NWS can return None for value, force it to an integer
                day_pop_raw = period.get('probabilityOfPrecipitation', {})
                day_pop = int(day_pop_raw.get('value') or 0) if day_pop_raw else 0
                
                night_pop_raw = night_period.get('probabilityOfPrecipitation', {})
                night_pop = int(night_pop_raw.get('value') or 0) if night_pop_raw else 0
                
                raw_chance = max(day_pop, night_pop)
                daily_forecast.append({
                    'name': day_name,
                    'high': period['temperature'],
                    'low': night_period['temperature'],
                    'heat_index': heat_idx,
                    'humidity': hum_idx,
                    'rain_chance': round(raw_chance / 5) * 5,
                    'short_forecast': period['shortForecast']
                })
                skip_next = True
            elif not period['isDaytime']:
                night_pop_raw = period.get('probabilityOfPrecipitation', {})
                night_pop = int(night_pop_raw.get('value') or 0) if night_pop_raw else 0
                
                daily_forecast.append({
                    'name': day_name,
                    'high': afternoon_temp,
                    'low': period['temperature'],
                    'heat_index': heat_idx,
                    'humidity': hum_idx,
                    'rain_chance': round(night_pop / 5) * 5,
                    'short_forecast': period['shortForecast']
                })

        # 3. Process Active NWS Alerts for Exact Location
        active_alerts = get_active_alerts(headers)

        # 4. Determine if Radar should be displayed
        show_radar = False
        
        # Strategy 1: Active radar sweep timing (Precipitation Mode)
        if get_radar_sweep_status() is True:
            show_radar = True
            
        # Strategy 2: Fallback to NWS PoP and Keywords
        if not show_radar and len(hourly_display) > 0:
            current_pop = hourly_display[0].get('pop', 0)
            current_forecast = daily_forecast[0]['short_forecast'].lower() if len(daily_forecast) > 0 else ""
            
            # Trigger on 20% PoP OR explicit forecast keywords
            if current_pop >= 20 or any(word in current_forecast for word in ['rain', 'shower', 'storm', 'thunder', 'drizzle']):
                show_radar = True

        # Override: Always show radar if there is an active weather warning
        if len(active_alerts) > 0:
            show_radar = True

        # Config Override: Suppress radar visibility if disabled in config
        if not is_radar_enabled():
            show_radar = False

        return {
            "morning_temp": morning_temp,
            "afternoon_temp": afternoon_temp,
            "current_temp": current_temp,
            "feels_like": feels_like,
            "humidity": current_humidity,
            "will_rain": will_rain,
            "clothing": clothing,
            "hourly": hourly_display,
            "daily": daily_forecast,
            "alerts": active_alerts,
            "show_radar": show_radar
        }
    except Exception as e:
        print(f"Weather API Error: {e}")
        return None


# ==========================================
# SCHOOL LUNCH LOGIC
# ==========================================
def load_menu():
    if os.path.exists(MENU_FILE):
        try:
            with open(MENU_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Menu Error: {e}")
    return {}

def save_menu(menu_data, merge=True):
    if merge:
        current_data = load_menu()
        current_data.update(menu_data)
        data_to_write = current_data
    else:
        data_to_write = menu_data
    with open(MENU_FILE, 'w') as f:
        json.dump(data_to_write, f, indent=2)
    # Refresh cache immediately
    CACHE['lunch'] = get_lunch_menu()

def get_lunch_menu():
    try:
        menu_data = load_menu()
        now_dt = datetime.datetime.now(ZoneInfo(TIMEZONE))
        today_str = now_dt.strftime('%Y-%m-%d')
        today_weekday = now_dt.weekday()

        if today_weekday == 4:  # Friday -> Next school day is Monday (+3 days)
            next_school_dt = now_dt + datetime.timedelta(days=3)
            next_menu_title = "Monday's Menu"
            next_button_label = "MONDAY"
        elif today_weekday == 5:  # Saturday -> Next school day is Monday (+2 days)
            next_school_dt = now_dt + datetime.timedelta(days=2)
            next_menu_title = "Monday's Menu"
            next_button_label = "MONDAY"
        elif today_weekday == 6:  # Sunday -> Next school day is Monday (+1 day)
            next_school_dt = now_dt + datetime.timedelta(days=1)
            next_menu_title = "Monday's Menu"
            next_button_label = "MONDAY"
        else:  # Mon-Thu -> Tomorrow (+1 day)
            next_school_dt = now_dt + datetime.timedelta(days=1)
            next_menu_title = "Tomorrow's Menu"
            next_button_label = "TOMORROW"

        next_school_str = next_school_dt.strftime('%Y-%m-%d')
        next_school_date_display = next_school_dt.strftime('%A, %b %d')

        today_menu = menu_data.get(today_str, None)
        next_menu = menu_data.get(next_school_str, None)

        return {
            'today': today_menu,
            'tomorrow': next_menu,
            'tomorrow_title': next_menu_title,
            'tomorrow_button_label': next_button_label,
            'tomorrow_date_str': next_school_date_display
        }
    except Exception as e:
        print(f"Menu Error: {e}")
        return {
            'today': None,
            'tomorrow': None,
            'tomorrow_title': "Tomorrow's Menu",
            'tomorrow_button_label': "TOMORROW",
            'tomorrow_date_str': ''
        }

def load_activities():
    if os.path.exists(ACTIVITY_FILE):
        try:
            with open(ACTIVITY_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading activities: {e}")
    return {}

def save_activities(data):
    with open(ACTIVITY_FILE, 'w') as f:
        json.dump(data, f, indent=2)
    # Refresh cache immediately
    CACHE['activity'] = get_school_activity()

def get_school_activity():
    try:
        activity_data = load_activities()
        now_dt = datetime.datetime.now(ZoneInfo(TIMEZONE))
        today_name = now_dt.strftime('%A')
        today_weekday = now_dt.weekday()

        if today_weekday in (5, 6):  # Saturday or Sunday
            today_activity = "No School Today!"
        else:
            today_activity = activity_data.get(today_name, "No School Today!")

        if today_weekday == 4:  # Friday -> Next school day is Monday (+3 days)
            next_school_dt = now_dt + datetime.timedelta(days=3)
            next_activity_title = "Monday's Activities"
            next_button_label = "MONDAY"
        elif today_weekday == 5:  # Saturday -> Next school day is Monday (+2 days)
            next_school_dt = now_dt + datetime.timedelta(days=2)
            next_activity_title = "Monday's Activities"
            next_button_label = "MONDAY"
        elif today_weekday == 6:  # Sunday -> Next school day is Monday (+1 day)
            next_school_dt = now_dt + datetime.timedelta(days=1)
            next_activity_title = "Monday's Activities"
            next_button_label = "MONDAY"
        else:  # Mon-Thu -> Tomorrow (+1 day)
            next_school_dt = now_dt + datetime.timedelta(days=1)
            next_activity_title = "Tomorrow's Activities"
            next_button_label = "TOMORROW"

        next_day_name = next_school_dt.strftime('%A')
        next_school_date_display = next_school_dt.strftime('%A, %b %d')
        next_activity = activity_data.get(next_day_name, "No School Activities")

        return {
            'today': today_activity,
            'tomorrow': next_activity,
            'tomorrow_title': next_activity_title,
            'tomorrow_button_label': next_button_label,
            'tomorrow_date_str': next_school_date_display
        }
    except Exception as e:
        print(f"Activity Error: {e}")
        return {
            'today': "Unavailable",
            'tomorrow': None,
            'tomorrow_title': "Tomorrow's Activities",
            'tomorrow_button_label': "TOMORROW",
            'tomorrow_date_str': ''
        }

# ==========================================
# BACKGROUND POLLING & CACHING
# ==========================================
CACHE = {'events': None, 'weather': None, 'lunch': None, 'activity': None}

def poll_service(service_name, fetch_func, interval_seconds):
    while True:
        try:
            CACHE[service_name] = fetch_func()
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {service_name.capitalize()} cache updated.")
        except Exception as e:
            print(f"{service_name.capitalize()} Update Error: {e}")
        time.sleep(interval_seconds)

def refresh_events_cache():
    try:
        new_events = get_events()
        if new_events and not (isinstance(new_events, list) and new_events and new_events[0].get('summary', '').startswith('Auth Error')):
            CACHE['events'] = new_events
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Events cache forcefully updated.")
    except Exception as e:
        print(f"Error forcefully updating events cache: {e}")

# Spin up isolated daemon threads so independent failures/delays don't block each other
threading.Thread(target=poll_service, args=('events', get_events, CAL_POLL), daemon=True).start()
threading.Thread(target=poll_service, args=('weather', get_weather, WX_POLL), daemon=True).start()
threading.Thread(target=poll_service, args=('lunch', get_lunch_menu, LUNCH_POLL), daemon=True).start()
threading.Thread(target=poll_service, args=('activity', get_school_activity, ACTIVITY_POLL), daemon=True).start()

try:
    from sync_basketball import sync_basketball_games
except ImportError:
    sync_basketball_games = None

def basketball_sync_loop():
    time.sleep(15)
    while True:
        try:
            if sync_basketball_games:
                print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Running automatic basketball sync...")
                res = sync_basketball_games(dry_run=False)
                if res.get('created') or res.get('updated'):
                    refresh_events_cache()
        except Exception as e:
            print(f"[Basketball Sync Daemon Error]: {e}")
        time.sleep(3600)

threading.Thread(target=basketball_sync_loop, daemon=True).start()

if os.getenv('AUTO_LOCATION_ENABLED', 'true').lower() in ('true', '1', 'yes'):
    try:
        from location_resolver import start_location_resolver_daemon
        start_location_resolver_daemon(offset_seconds=60, on_update_callback=refresh_events_cache)
        print("[INFO] Started Location Resolver daemon thread (triggers 60s after calendar poll).")
    except Exception as e:
        print(f"[WARNING] Failed to start Location Resolver daemon: {e}")

# ==========================================
# FLASK ROUTES
# ==========================================
@app.route('/')
def index():
    # Serve from cache, fallback to direct fetch if cache is still initializing on boot
    events = CACHE['events'] or get_events()
    weather = CACHE['weather'] or get_weather()
    lunch_data = CACHE['lunch'] or get_lunch_menu()
    activity_data = CACHE['activity'] or get_school_activity()
    radar_enabled = is_radar_enabled()
    
    if isinstance(lunch_data, dict) and ('today' in lunch_data or 'tomorrow' in lunch_data):
        lunch = lunch_data.get('today')
        tomorrow_lunch = lunch_data.get('tomorrow')
        tomorrow_title = lunch_data.get('tomorrow_title', "Tomorrow's Menu")
        tomorrow_button_label = lunch_data.get('tomorrow_button_label', "TOMORROW")
        tomorrow_date_str = lunch_data.get('tomorrow_date_str', '')
    else:
        lunch = lunch_data
        tomorrow_lunch = None
        tomorrow_title = "Tomorrow's Menu"
        tomorrow_button_label = "TOMORROW"
        tomorrow_date_str = ''

    if isinstance(activity_data, dict) and ('today' in activity_data or 'tomorrow' in activity_data):
        activity = activity_data.get('today')
        tomorrow_activity = activity_data.get('tomorrow')
        tomorrow_activity_title = activity_data.get('tomorrow_title', "Tomorrow's Activities")
        tomorrow_activity_button_label = activity_data.get('tomorrow_button_label', "TOMORROW")
        tomorrow_activity_date_str = activity_data.get('tomorrow_date_str', '')
    else:
        activity = activity_data
        tomorrow_activity = None
        tomorrow_activity_title = "Tomorrow's Activities"
        tomorrow_activity_button_label = "TOMORROW"
        tomorrow_activity_date_str = ''

    radar_lat, radar_lon = get_radar_coordinates()

    return render_template(
        f'{THEME}/index.html',
        events=events,
        weather=weather,
        lunch=lunch,
        tomorrow_lunch=tomorrow_lunch,
        tomorrow_title=tomorrow_title,
        tomorrow_button_label=tomorrow_button_label,
        tomorrow_date_str=tomorrow_date_str,
        activity=activity,
        tomorrow_activity=tomorrow_activity,
        tomorrow_activity_title=tomorrow_activity_title,
        tomorrow_activity_button_label=tomorrow_activity_button_label,
        tomorrow_activity_date_str=tomorrow_activity_date_str,
        home_lat=HOME_LAT,
        home_lon=HOME_LON,
        radar_lat=radar_lat,
        radar_lon=radar_lon,
        ui_refresh=UI_REFRESH,
        radar_enabled=radar_enabled,
        clothing_enabled=is_clothing_enabled(),
        entree_images=load_entree_images()
    )

@app.route('/api/calendars', methods=['GET', 'POST'])
def api_calendars():
    if request.method == 'POST':
        try:
            data = request.json or {}
            selected_ids = data.get('selected_calendar_ids')
            if not isinstance(selected_ids, list):
                return jsonify({"status": "error", "message": "selected_calendar_ids must be a list"}), 400
            
            settings = load_calendar_settings()
            settings['selected_calendar_ids'] = selected_ids
            if 'calendar_metadata' in data and isinstance(data['calendar_metadata'], dict):
                settings['calendar_metadata'].update(data['calendar_metadata'])
            save_calendar_settings(settings)

            # Force immediate cache update across threads
            threading.Thread(target=refresh_events_cache, daemon=True).start()

            return jsonify({
                "status": "success",
                "message": f"Successfully updated {len(selected_ids)} calendar(s)",
                "selected_calendar_ids": selected_ids
            })
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    # GET request
    creds = None
    token_file = os.path.join(BASE_DIR, 'token.json')
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        try:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                return jsonify({
                    "status": "needs_reauth",
                    "error_type": "no_token",
                    "message": "Authentication token missing or invalid. Please run 'python get_token.py' in the familycenter folder."
                }), 401
        except Exception as e:
            return jsonify({
                "status": "needs_reauth",
                "error_type": "token_expired",
                "message": f"Token expired or refresh failed: {e}. Please run 'python get_token.py'."
            }), 401

    try:
        service = build('calendar', 'v3', credentials=creds)
        cal_list_res = service.calendarList().list().execute()
        items = cal_list_res.get('items', [])

        settings = load_calendar_settings()
        selected_ids = set(settings.get('selected_calendar_ids', []))
        if not selected_ids and CALENDAR_ID:
            selected_ids = {CALENDAR_ID}

        calendars = []
        for c in items:
            cid = c.get('id')
            is_primary = c.get('primary', False)
            calendars.append({
                "id": cid,
                "summary": c.get('summary', 'Untitled Calendar'),
                "description": c.get('description', ''),
                "primary": is_primary,
                "backgroundColor": c.get('backgroundColor', '#1a73e8'),
                "foregroundColor": c.get('foregroundColor', '#ffffff'),
                "accessRole": c.get('accessRole', ''),
                "selected": (cid in selected_ids) or (is_primary and not selected_ids)
            })

        # Sort: primary first, then alphabetical
        calendars.sort(key=lambda x: (not x['primary'], x['summary'].lower()))

        return jsonify({
            "status": "success",
            "calendars": calendars,
            "selected_calendar_ids": list(selected_ids)
        })
    except Exception as e:
        err_str = str(e)
        if "insufficientPermissions" in err_str or "Request had insufficient authentication scopes" in err_str:
            return jsonify({
                "status": "needs_reauth",
                "error_type": "insufficient_scope",
                "message": "Google permissions need to be updated to list calendars. Run 'python get_token.py' in familycenter to re-authorize with calendar list access."
            }), 403
        return jsonify({
            "status": "error",
            "message": f"Failed to retrieve calendars: {err_str}"
        }), 500

@app.route('/api/sync-basketball', methods=['POST', 'GET'])
def api_sync_basketball():
    if not sync_basketball_games:
        return jsonify({"status": "error", "message": "sync_basketball module not available"}), 500
    try:
        results = sync_basketball_games(dry_run=False)
        refresh_events_cache()
        return jsonify({
            "status": "success",
            "summary": {
                "created": len(results.get('created', [])),
                "updated": len(results.get('updated', [])),
                "skipped": len(results.get('skipped', [])),
                "errors": len(results.get('errors', []))
            },
            "details": results
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/bg-settings', methods=['GET', 'POST'])
def bg_settings():
    if request.method == 'POST':
        save_bg_settings(request.json)
        return jsonify({"status": "success"})
    
    settings = load_bg_settings()
    available = []
    
    print(f"\n--- DEBUG: Checking for backgrounds ---")
    print(f"Looking in directory: {app.config['UPLOAD_FOLDER']}")
    
    if os.path.exists(app.config['UPLOAD_FOLDER']):
        all_files = os.listdir(app.config['UPLOAD_FOLDER'])
        print(f"Total files found in directory: {len(all_files)}")
        available = [f for f in all_files if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.heic', '.jfif'))]
        print(f"Valid image files identified: {available}")
    else:
        print("WARNING: The upload directory does not exist!")
        
    settings['available'] = available
    print(f"---------------------------------------\n")
    return jsonify(settings)

@app.route('/api/upload-bg', methods=['POST'])
def upload_bg():
    print(f"\n--- DEBUG: Starting File Upload ---")
    if 'file' not in request.files:
        print("Upload Error: No file part in request")
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        print("Upload Error: No selected file")
        return jsonify({"error": "No selected file"}), 400
    if file:
        print(f"Original filename received: {file.filename}")
        filename = secure_filename(file.filename)
        
        # Prevent collision with the default UI background image
        if filename.lower() == 'background.jpg':
            filename = f"custom_bg_{int(time.time())}.jpg"
            print(f"Filename collision detected, renamed to: {filename}")
            
        if not filename or filename.startswith('.'):
            ext = file.filename.rsplit('.', 1)[1] if '.' in file.filename else 'jpg'
            filename = f"bg_{int(time.time())}.{ext}"
            print(f"Filename stripped, generated timestamp name: {filename}")
            
        save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        print(f"Attempting to save to physical path: {save_path}")
        
        try:
            file.save(save_path)
            print(f"SUCCESS: File successfully written to disk!")
        except Exception as e:
            print(f"CRITICAL ERROR writing file: {e}")
            
        print(f"-----------------------------------\n")
        return jsonify({"status": "success", "filename": filename})

@app.route('/api/activities', methods=['GET', 'POST'])
def api_activities():
    if request.method == 'POST':
        try:
            data = request.json
            if not isinstance(data, dict):
                return jsonify({"error": "Invalid data format"}), 400
            save_activities(data)
            return jsonify({"status": "success", "data": data})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify(load_activities())

@app.route('/api/menu', methods=['GET'])
def api_get_menu():
    menu_data = load_menu()
    today_str = datetime.datetime.now(ZoneInfo(TIMEZONE)).strftime('%Y-%m-%d')
    upcoming = []
    for d in sorted(menu_data.keys()):
        if d >= today_str:
            upcoming.append({"date": d, "entrees": menu_data[d].get("entrees", []), "sides": menu_data[d].get("sides", [])})
            if len(upcoming) >= 15:
                break
    return jsonify({
        "total_days": len(menu_data),
        "upcoming": upcoming,
        "menu": menu_data
    })

@app.route('/api/upload-menu', methods=['POST'])
def api_upload_menu():
    try:
        menu_data = None
        if 'file' in request.files:
            file = request.files['file']
            if file.filename == '':
                return jsonify({"error": "No file selected"}), 400
            content = file.read().decode('utf-8')
            menu_data = json.loads(content)
        elif request.is_json:
            menu_data = request.json
        else:
            return jsonify({"error": "No JSON file or payload provided"}), 400

        if not isinstance(menu_data, dict):
            return jsonify({"error": "Root element must be a JSON object mapping date strings to meals"}), 400

        # Validate entries
        date_pattern = re.compile(r'^\d{4}-\d{2}-\d{2}$')
        valid_entries = {}
        for k, v in menu_data.items():
            if not isinstance(k, str) or not date_pattern.match(k.strip()):
                continue
            if not isinstance(v, dict):
                continue
            entrees = v.get('entrees', [])
            sides = v.get('sides', [])
            if not isinstance(entrees, list):
                entrees = [str(entrees)]
            if not isinstance(sides, list):
                sides = [str(sides)]
            valid_entries[k.strip()] = {
                "entrees": entrees,
                "sides": sides
            }

        if not valid_entries:
            return jsonify({"error": "No valid meal date entries found (expected keys formatted as YYYY-MM-DD)"}), 400

        save_menu(valid_entries, merge=True)

        return jsonify({
            "status": "success",
            "count": len(valid_entries),
            "total_days": len(load_menu())
        })
    except json.JSONDecodeError as jde:
        return jsonify({"error": f"Invalid JSON syntax: {jde.msg}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/entree-images', methods=['GET'])
def api_get_entree_images():
    menu_data = load_menu()
    all_entrees = sorted(list(set(e for day in menu_data.values() for e in day.get('entrees', []) if e)))
    mapping = load_entree_images()
    return jsonify({
        "images": mapping,
        "entrees": all_entrees
    })

@app.route('/api/upload-entree-photo', methods=['POST'])
def api_upload_entree_photo():
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file uploaded"}), 400
        file = request.files['file']
        entree_name = request.form.get('entree_name', '').strip()
        if not entree_name:
            return jsonify({"error": "No entree name provided"}), 400
        if file.filename == '':
            return jsonify({"error": "No selected file"}), 400

        ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else 'jpg'
        if ext not in ('jpg', 'jpeg', 'png', 'webp', 'gif'):
            ext = 'jpg'
        filename = f"{slugify_entree(entree_name)}.{ext}"
        filepath = os.path.join(ENTREE_IMG_FOLDER, filename)
        file.save(filepath)

        mapping = load_entree_images()
        mapping[entree_name] = f"/static/entrees/{filename}"
        save_entree_images(mapping)

        return jsonify({
            "status": "success",
            "entree_name": entree_name,
            "url": f"/static/entrees/{filename}"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)