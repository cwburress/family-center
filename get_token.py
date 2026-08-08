import os
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ['https://www.googleapis.com/auth/calendar.events']

def main():
    flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
    auth_url, _ = flow.authorization_url(prompt='consent')
    print(f"\nAuthorization URL:\n{auth_url}\n", flush=True)
    creds = flow.run_local_server(port=8080, open_browser=False)
    
    with open('token.json', 'w') as token:
        token.write(creds.to_json())
    print("Success! token.json has been created.", flush=True)

if __name__ == '__main__':
    main()