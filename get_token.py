"""
get_token.py — run this ONCE on your local machine to get a Google OAuth
refresh token. Paste the printed JSON into the GOOGLE_CREDENTIALS_JSON
GitHub secret.

Usage:
  pip install google-auth-oauthlib
  python get_token.py
"""

import json
from google_auth_oauthlib.flow import InstalledAppFlow

# Paste your client_id and client_secret from the Google Cloud Console here
CLIENT_ID     = "YOUR_CLIENT_ID_HERE"
CLIENT_SECRET = "YOUR_CLIENT_SECRET_HERE"

SCOPES = ["https://www.googleapis.com/auth/tasks.readonly"]

client_config = {
    "installed": {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
    }
}

flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
creds = flow.run_local_server(port=0)

output = {
    "token":         creds.token,
    "refresh_token": creds.refresh_token,
    "client_id":     CLIENT_ID,
    "client_secret": CLIENT_SECRET,
}

print("\n✅ Copy this JSON and paste it into your GOOGLE_CREDENTIALS_JSON GitHub secret:\n")
print(json.dumps(output, indent=2))
