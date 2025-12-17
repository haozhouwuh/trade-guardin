# File: schwab_token_manager.py

import requests
import json
from typing import Optional

# --- Configuration for API Token Server ---
TOKEN_SERVER = "http://127.0.0.1:5000" # Your local token server URL

def fetch_schwab_token() -> Optional[str]:
    """
    Fetches the Schwab API access token from the local token server.
    This is a central utility function to be used by other data fetcher modules.
    """
    try:
        resp = requests.get(f"{TOKEN_SERVER}/token")
        resp.raise_for_status()
        token_data = resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            print("Error: 'access_token' key not found in token server response.")
            return None
        return access_token
    except requests.exceptions.RequestException as e:
        print(f"Error fetching Schwab token: {e}")
    except json.JSONDecodeError:
        print(f"Error decoding JSON from token server. Response: {resp.text}")
    return None

# --- Main block for standalone testing ---
if __name__ == "__main__":
    print("--- Testing Schwab Token Manager ---")
    token = fetch_schwab_token()
    if token:
        print("Successfully fetched an access token.")
        # print(f"Token (first 15 chars): {token[:15]}...") # Uncomment for more verbose testing
    else:
        print("Failed to fetch an access token.")
    print("--- Test Complete ---")