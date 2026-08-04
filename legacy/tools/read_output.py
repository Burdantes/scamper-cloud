import json

def read_output():
    final_output = []
    with open('datasets/testingmerge.json', 'r') as infile:
        for line in infile:
            specific_row = json.loads(line)
            final_output.append(specific_row)
        # final_output = json.load(infile)
    print(final_output)
    return final_output

google_cloud_regions = {
    "northamerica-northeast1": {"city": "Montréal", "country": "Canada", "lat": 45.5017, "lon": -73.5673},
    "us-central1": {"city": "Iowa", "country": "USA", "lat": 41.878, "lon": -93.0977},
    "us-east1": {"city": "South Carolina", "country": "USA", "lat": 33.8361, "lon": -81.1637},
    "us-east4": {"city": "Northern Virginia", "country": "USA", "lat": 38.9072, "lon": -77.0369},
    "us-west1": {"city": "Oregon", "country": "USA", "lat": 43.8041, "lon": -120.5542},
    "southamerica-east1": {"city": "São Paulo", "country": "Brazil", "lat": -23.5505, "lon": -46.6333},
    "europe-north1": {"city": "Finland", "country": "Finland", "lat": 61.9241, "lon": 25.7482},
    "europe-west1": {"city": "Belgium", "country": "Belgium", "lat": 50.5039, "lon": 4.4699},
    "europe-west2": {"city": "London", "country": "UK", "lat": 51.5074, "lon": -0.1278},
    "europe-west3": {"city": "Frankfurt", "country": "Germany", "lat": 50.1109, "lon": 8.6821},
    "europe-west4": {"city": "Netherlands", "country": "Netherlands", "lat": 52.1326, "lon": 5.2913},
    "europe-west6": {"city": "Zürich", "country": "Switzerland", "lat": 47.3769, "lon": 8.5417},
    "asia-east1": {"city": "Taiwan", "country": "Taiwan", "lat": 23.6978, "lon": 120.9605},
    "asia-east2": {"city": "Hong Kong", "country": "Hong Kong", "lat": 22.3193, "lon": 114.1694},
    "asia-northeast1": {"city": "Tokyo", "country": "Japan", "lat": 35.6895, "lon": 139.6917},
    "asia-northeast2": {"city": "Osaka", "country": "Japan", "lat": 34.6937, "lon": 135.5023},
    "asia-northeast3": {"city": "Seoul", "country": "South Korea", "lat": 37.5665, "lon": 126.978},
    "asia-south1": {"city": "Mumbai", "country": "India", "lat": 19.076, "lon": 72.8777},
    "asia-southeast1": {"city": "Singapore", "country": "Singapore", "lat": 1.3521, "lon": 103.8198},
    "asia-southeast2": {"city": "Jakarta", "country": "Indonesia", "lat": -6.2088, "lon": 106.8456},
    "australia-southeast1": {"city": "Sydney", "country": "Australia", "lat": -33.8688, "lon": 151.2093},
}

print(read_output())
