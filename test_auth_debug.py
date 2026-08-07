import requests
from modules.utils import JACKPOT_CITY_ENDPOINTS, HEADERS, add_za_country_code

def test_auth():
    username = "999912345"
    password = "password123"
    formatted = add_za_country_code(username)
    payload = {
        "username": formatted,
        "password": password,
        "countryCode": "ZA",
        "sessionMetadata": {
            "sessionTrackingToken": "643B3CA7C0E72504E48E77E0F12A212693DC75B0",
            "appType": "",
            "appsFlyerExternalRef": "",
            "uip": "115.110.105.36"
        }
    }
    
    print("URL:", JACKPOT_CITY_ENDPOINTS["AUTH"])
    print("Payload:", payload)
    print("Headers:", HEADERS["AUTH"])
    
    try:
        resp = requests.post(
            JACKPOT_CITY_ENDPOINTS["AUTH"], 
            headers=HEADERS["AUTH"], 
            json=payload, 
            timeout=10, 
            verify=False
        )
        print("Status code:", resp.status_code)
        print("Response headers:", resp.headers)
        print("Response text:", resp.text[:1000]) # Print first 1000 characters
        
        try:
            data = resp.json()
            print("Parsed JSON:", data)
        except Exception as e:
            print("Failed to parse JSON:", e)
    except Exception as e:
        print("Request failed:", e)

if __name__ == "__main__":
    test_auth()
