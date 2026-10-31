"""Run ONCE on your computer after Google approves Business Profile API access.

1. In Google Cloud Console → Credentials → Create OAuth client ID → type "Desktop app".
2. Download the JSON and save it next to this script as client_secret.json
3. pip install google-auth-oauthlib
4. python get_gbp_refresh_token.py
   → sign in with the Google account that manages the branches.
5. Copy the printed values into GitHub → Settings → Secrets → Actions:
   GBP_CLIENT_ID, GBP_CLIENT_SECRET, GBP_REFRESH_TOKEN
6. In config.json change  "source": "apify"  →  "source": "gbp"
"""
from google_auth_oauthlib.flow import InstalledAppFlow

flow = InstalledAppFlow.from_client_secrets_file(
    "client_secret.json",
    scopes=["https://www.googleapis.com/auth/business.manage"],
)
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
print("\nGBP_CLIENT_ID     =", creds.client_id)
print("GBP_CLIENT_SECRET =", creds.client_secret)
print("GBP_REFRESH_TOKEN =", creds.refresh_token)
