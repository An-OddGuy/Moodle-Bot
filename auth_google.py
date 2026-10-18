from google_auth_oauthlib.flow import InstalledAppFlow

flow = InstalledAppFlow.from_client_secrets_file(
    "credentials.json", ["https://www.googleapis.com/auth/calendar.events"])
creds = flow.run_local_server(port=0)
open("token.json", "w").write(creds.to_json())