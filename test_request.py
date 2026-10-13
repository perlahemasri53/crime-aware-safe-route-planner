import requests

url = "http://127.0.0.1:5000/getRoutes"

# ✅ Use addresses, not lat/lng
data = {
    "start": "Nalgonda Bus Stand, Telangana",
    "end": "Hyderabad Charminar, Telangana"
}

response = requests.post(url, json=data)

print("Status:", response.status_code)

try:
    # If backend returns valid JSON
    print("Response:", response.json())
except ValueError:
    # If backend returns error or non‑JSON text
    print("Raw Response:", response.text)
