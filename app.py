import datetime
import json
import os
import re
import requests
import threading
import time
from zoneinfo import ZoneInfo
from flask import Flask, render_template
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from dotenv import load_dotenv

# Load variables from .env
load_dotenv()

app = Flask(__name__)

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
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            return [{"summary": "Auth Error: Token missing or invalid", "start": {}}]
 
    try:
        service = build('calendar', 'v3', credentials=creds)
        now = datetime.datetime.utcnow().isoformat() + 'Z'  # 'Z' indicates UTC time
        
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
            'day': today_date.strftime('%d')
        }

        for event in raw_events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            end_raw = event.get('end', {}).get('dateTime', event.get('end', {}).get('date'))
            
            try:
                if 'T' in start:
                    dt = datetime.datetime.fromisoformat(start.replace('Z', '+00:00'))
                    dt = dt.astimezone(ZoneInfo(TIMEZONE))
                    time_str = dt.strftime('%-I:%M%p').lower()
                else:
                    dt = datetime.datetime.fromisoformat(start)
                    time_str = "All day"
                
                end_time_str = ""
                if end_raw and 'T' in end_raw and time_str != "All day":
                    end_dt = datetime.datetime.fromisoformat(end_raw.replace('Z', '+00:00'))
                    end_dt = end_dt.astimezone(ZoneInfo(TIMEZONE))
                    end_time_str = end_dt.strftime('%-I:%M%p').lower()
                
                day_num = dt.strftime('%-d')
                month_day_str = dt.strftime('%b, %a').upper()
                full_date = dt.strftime('%Y-%m-%d')
                is_today = (full_date == today_str)
                
            except Exception:
                time_str = start
                end_time_str = ""
                day_num = ""
                month_day_str = ""
                full_date = ""
                is_today = False
                
            event_data = {
                'summary': event.get('summary', 'Busy'),
                'time_str': time_str,
                'end_time_str': end_time_str,
                'day_num': day_num,
                'month_day_str': month_day_str,
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
            
        return {'today': today_events, 'future': future_events, 'today_display': today_display}

    except Exception as e:
        return [{"summary": f"API Error: {e}", "start": {}}]


# ==========================================
# WEATHER & CLOTHING LOGIC
# ==========================================
def get_clothing_recommendation(morning_temp, afternoon_temp, will_rain):
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
    end_dt = datetime.datetime.utcnow()
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
        clothing = get_clothing_recommendation(morning_temp, afternoon_temp, will_rain)
        
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
                
            dt = datetime.datetime.fromisoformat(period['startTime'])
            day_name = dt.strftime('%a')

            if period['isDaytime'] and i + 1 < len(daily_periods_raw):
                night_period = daily_periods_raw[i+1]
                
                # NWS can return None for value, force it to an integer
                day_pop_raw = period.get('probabilityOfPrecipitation', {})
                day_pop = int(day_pop_raw.get('value') or 0) if day_pop_raw else 0
                
                night_pop_raw = night_period.get('probabilityOfPrecipitation', {})
                night_pop = int(night_pop_raw.get('value') or 0) if night_pop_raw else 0
                
                daily_forecast.append({
                    'name': day_name,
                    'high': period['temperature'],
                    'low': night_period['temperature'],
                    'rain_chance': max(day_pop, night_pop),
                    'short_forecast': period['shortForecast']
                })
                skip_next = True
            elif not period['isDaytime']:
                night_pop_raw = period.get('probabilityOfPrecipitation', {})
                night_pop = int(night_pop_raw.get('value') or 0) if night_pop_raw else 0
                
                daily_forecast.append({
                    'name': 'Tonight',
                    'high': '--',
                    'low': period['temperature'],
                    'rain_chance': night_pop,
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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)