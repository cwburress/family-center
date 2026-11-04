import os
from google_auth_oauthlib.flow import InstalledAppFlow

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_PATH = os.path.join(BASE_DIR, 'credentials.json')
TOKEN_PATH = os.path.join(BASE_DIR, 'token.json')

SCOPES = [
    'https://www.googleapis.com/auth/calendar.readonly',
    'https://www.googleapis.com/auth/calendar.events'
]

def main():
    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
    creds = flow.run_local_server(
        port=8080,
        open_browser=True,
        prompt='consent',
        access_type='offline'
    )
    
    with open(TOKEN_PATH, 'w') as token:
        token.write(creds.to_json())
    print("\nSuccess! token.json has been created with calendar list permissions.", flush=True)

if __name__ == '__main__':
    main()
