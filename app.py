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

def load_bg_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                settings = json.load(f)
                # Ensure new keys exist for older configs
                if "radar_loop" not in settings:
                    settings["radar_loop"] = True
                if "radar_reset_interval" not in settings:
                    settings["radar_reset_interval"] = 10
                if "schedule_reset_interval" not in settings:
                    settings["schedule_reset_interval"] = 15
                return settings
        except Exception:
            pass
    return {"rotate": False, "interval": 300, "selected": ["background.jpg"], "radar_loop": True, "radar_reset_interval": 10, "schedule_reset_interval": 15}

def save_bg_settings(settings):
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(settings, f)

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
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
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)

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
        
        # Pull the next 30 events to ensure we have enough for both views
        events_result = service.events().list(calendarId=CALENDAR_ID, timeMin=now,
                                              maxResults=30, singleEvents=True,
                                              orderBy='startTime').execute()
        
        raw_events = events_result.get('items', [])
        future_events = []
        
        # Build the 4-day focus window starting from today
        # Build the single-day focus window for Today
        current_time = datetime.datetime.now()
        today_date = current_time.date()
        today_str = today_date.strftime('%Y-%m-%d')
        
        today_events = []
        
        # Setup the display strings for the large Today header
        today_display = {
            'month': today_date.strftime('%B').upper(),
            'day': today_date.strftime('%d'),
            'weekday': today_date.strftime('%A').upper()
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
                    'location': event.get('location', '')
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
    
    # The EPA/WHO recommends sun protection at UV Index 3+, but 5+ is a more practical "Moderate/High" trigger
    if max_uv >= 5.0:
        outfit.append("Wear Sunscreen! ☀️")

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
        
        # Grab current temp and max UV index from Open-Meteo for exact coordinates
        max_uv = 0
        current_temp = None
        if HOME_LAT != '0.0' and HOME_LON != '0.0':
            try:
                om_url = f"https://api.open-meteo.com/v1/forecast?latitude={HOME_LAT}&longitude={HOME_LON}&current=temperature_2m&daily=uv_index_max&timezone=auto&forecast_days=1&temperature_unit=fahrenheit"
                om_resp = requests.get(om_url, timeout=10)
                if om_resp.status_code == 200:
                    om_data = om_resp.json()
                    max_uv = om_data.get('daily', {}).get('uv_index_max', [0])[0] or 0
                    
                    # Grab the current temp and round it to a clean integer
                    raw_temp = om_data.get('current', {}).get('temperature_2m')
                    if raw_temp is not None:
                        current_temp = round(raw_temp)
            except Exception as e:
                print(f"Open-Meteo API Error: {e}")

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

        return {
            "morning_temp": morning_temp,
            "afternoon_temp": afternoon_temp,
            "current_temp": current_temp,
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
def get_lunch_menu():
    try:
        with open('menu.json', 'r') as f:
            menu_data = json.load(f)
        
        today_str = datetime.datetime.now().strftime('%Y-%m-%d')
        return menu_data.get(today_str, None)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Menu Error: {e}")
        return None

def get_school_activity():
    try:
        with open('activity.json', 'r') as f:
            activity_data = json.load(f)
        
        today_name = datetime.datetime.now(ZoneInfo(TIMEZONE)).strftime('%A')
        return activity_data.get(today_name, "None")
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Activity Error: {e}")
        return "Unavailable"

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

# Spin up isolated daemon threads so independent failures/delays don't block each other
threading.Thread(target=poll_service, args=('events', get_events, CAL_POLL), daemon=True).start()
threading.Thread(target=poll_service, args=('weather', get_weather, WX_POLL), daemon=True).start()
threading.Thread(target=poll_service, args=('lunch', get_lunch_menu, LUNCH_POLL), daemon=True).start()
threading.Thread(target=poll_service, args=('activity', get_school_activity, ACTIVITY_POLL), daemon=True).start()

# ==========================================
# FLASK ROUTES
# ==========================================
@app.route('/')
def index():
    # Serve from cache, fallback to direct fetch if cache is still initializing on boot
    events = CACHE['events'] or get_events()
    weather = CACHE['weather'] or get_weather()
    lunch = CACHE['lunch'] or get_lunch_menu()
    activity = CACHE['activity'] or get_school_activity()
    
    return render_template(f'{THEME}/index.html', events=events, weather=weather, lunch=lunch, activity=activity, home_lat=HOME_LAT, home_lon=HOME_LON, ui_refresh=UI_REFRESH)

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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)