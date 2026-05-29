import datetime
import json
import os
import requests
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
        current_time = datetime.datetime.now()
        today_date = current_time.date()
        today_str = today_date.strftime('%Y-%m-%d')
        
        focus_days = []
        for i in range(4):
            target_date = today_date + datetime.timedelta(days=i)
            if i == 0:
                day_name = "Today"
            elif i == 1:
                day_name = "Tomorrow"
            else:
                day_name = target_date.strftime('%A')
                
            focus_days.append({
                'full_date': target_date.strftime('%Y-%m-%d'),
                'name': day_name,
                'short_date': target_date.strftime('%b %-d').upper(),
                'events': []
            })

        for event in raw_events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            try:
                if 'T' in start:
                    dt = datetime.datetime.fromisoformat(start.replace('Z', '+00:00'))
                    time_str = dt.strftime('%-I:%M%p').lower()
                else:
                    dt = datetime.datetime.fromisoformat(start)
                    time_str = "All day"
                
                day_num = dt.strftime('%-d')
                month_day_str = dt.strftime('%b, %a').upper()
                full_date = dt.strftime('%Y-%m-%d')
                is_today = (full_date == today_str)
                
            except Exception:
                time_str = start
                day_num = ""
                month_day_str = ""
                full_date = ""
                is_today = False
                
            event_data = {
                'summary': event.get('summary', 'Busy'),
                'time_str': time_str,
                'day_num': day_num,
                'month_day_str': month_day_str,
                'full_date': full_date,
                'is_today': is_today
            }
            
            # Check if event falls in the next 4 days
            placed_in_focus = False
            for day in focus_days:
                if day['full_date'] == full_date:
                    day['events'].append(event_data)
                    placed_in_focus = True
                    break
            
            # If not in the 4-day window, it goes to the future schedule view
            if not placed_in_focus:
                future_events.append(event_data)
            
        return {'focus_days': focus_days, 'future': future_events}

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
                
            day_name = period['name']
            if day_name not in ["Today", "Tonight", "Tomorrow"]:
                day_name = day_name[:3]

            if period['isDaytime'] and i + 1 < len(daily_periods_raw):
                night_period = daily_periods_raw[i+1]
                day_pop = period.get('probabilityOfPrecipitation', {}).get('value') or 0
                night_pop = night_period.get('probabilityOfPrecipitation', {}).get('value') or 0
                
                daily_forecast.append({
                    'name': day_name,
                    'high': period['temperature'],
                    'low': night_period['temperature'],
                    'rain_chance': max(day_pop, night_pop),
                    'short_forecast': period['shortForecast']
                })
                skip_next = True
            elif not period['isDaytime']:
                daily_forecast.append({
                    'name': 'Tonight',
                    'high': '--',
                    'low': period['temperature'],
                    'rain_chance': period.get('probabilityOfPrecipitation', {}).get('value') or 0,
                    'short_forecast': period['shortForecast']
                })

        # 3. Process Active NWS Alerts for Exact Location
        active_alerts = get_active_alerts(headers)

        # 4. Determine if Radar should be displayed
        show_radar = False
        # Proxy: 30% or higher chance of rain in the current hour
        if len(hourly_display) > 0 and hourly_display[0].get('pop', 0) >= 30:
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

# ==========================================
# FLASK ROUTES
# ==========================================
@app.route('/')
def index():
    events = get_events()
    weather = get_weather()
    lunch = get_lunch_menu()
    return render_template(f'{THEME}/index.html', events=events, weather=weather, lunch=lunch, home_lat=HOME_LAT, home_lon=HOME_LON)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)