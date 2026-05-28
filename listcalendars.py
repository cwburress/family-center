import os.path
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

def main():
    if not os.path.exists('token.json'):
        print("Missing token.json. Run get_token.py first.")
        return
    
    creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    service = build('calendar', 'v3', credentials=creds)
    
    print("Fetching your calendars...\n")
    calendar_list = service.calendarList().list().execute()
    for entry in calendar_list.get('items', []):
        print(f"Name: {entry['summary']}")
        print(f"ID:   {entry['id']}\n")

if __name__ == '__main__':
    main()