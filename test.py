# test_api.py
import json
import requests
from datetime import datetime

BASE_URL = "http://localhost:1234"
OUT_FILE = "api_test_output.txt"

# KL city center for demo
DEFAULT_LAT = 3.139
DEFAULT_LNG = 101.6869

def write_header(f, title: str):
    f.write("\n" + "="*80 + "\n")
    f.write(f"{title}  [{datetime.now().isoformat()}]\n")
    f.write("="*80 + "\n")

def write_json(f, label: str, obj):
    f.write(f"\n## {label}\n")
    try:
        f.write(json.dumps(obj, indent=2, ensure_ascii=False))
    except Exception as e:
        f.write(f"<< Failed to serialize JSON: {e} >>")
    f.write("\n")

def post_json(path: str, payload: dict):
    url = f"{BASE_URL}{path}"
    r = requests.post(url, json=payload, timeout=20)
    return r.status_code, r.headers.get("content-type"), safe_json(r)

def get_json(path: str):
    url = f"{BASE_URL}{path}"
    r = requests.get(url, timeout=20)
    return r.status_code, r.headers.get("content-type"), safe_json(r)

def safe_json(r: requests.Response):
    try:
        return r.json()
    except Exception:
        return {"raw_text": r.text}

def main():
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        write_header(f, "Hospital API End-to-End Test")

        # 1) Nearby hospitals
        nearby_body = {"lat": DEFAULT_LAT, "lng": DEFAULT_LNG, "max_results": 10}
        sc, ct, data = post_json("/nearby-hospitals", nearby_body)
        write_json(f, f"POST /nearby-hospitals  (status={sc})", data)

        hospitals = (data or {}).get("hospitals", [])
        if not hospitals:
            f.write("\n!! No hospitals returned. Stop here.\n")
            return

        target = hospitals[0]
        hospital_id = target.get("hospital_id")
        f.write(f"\nSelected hospital_id for subsequent tests: {hospital_id}\n")

        # 2) Push frames via /camera-frame (simulated CCTV)
        cam_body = {
            "hospital_id": hospital_id,
            "images_b64": [JPEG_1x1_B64, JPEG_1x1_B64],
            "per_person_minutes": 10
        }
        sc, ct, data = post_json("/camera-frame", cam_body)
        write_json(f, f"POST /camera-frame  (status={sc})", data)

        # 3) GET latest camera estimate
        sc, ct, data = get_json(f"/camera-frame/{hospital_id}")
        write_json(f, f"GET /camera-frame/{hospital_id}  (status={sc})", data)

        # 4) Also test /wait-time (POST + GET)
        wt_body = {
            "hospital_id": hospital_id,
            "per_person_minutes": 12,
            "cameras": [
                {"image_b64": JPEG_1x1_B64, "camera_id": "laptop-1"},
                {"image_b64": JPEG_1x1_B64, "camera_id": "laptop-2"},
            ]
        }
        sc, ct, data = post_json("/wait-time", wt_body)
        write_json(f, f"POST /wait-time  (status={sc})", data)

        sc, ct, data = get_json(f"/wait-time/{hospital_id}")
        write_json(f, f"GET /wait-time/{hospital_id}  (status={sc})", data)

        # 5) Nearby again – should include current_estimated_wait_minutes
        sc, ct, data = post_json("/nearby-hospitals", nearby_body)
        write_json(f, f"POST /nearby-hospitals (after wait update)  (status={sc})", data)

        # 6) Smart nearby – try to enrich with one public image (ok if it fails; count=0 fallback)
        smart_body = {
            "lat": DEFAULT_LAT,
            "lng": DEFAULT_LNG,
            "limit": 5,
            "max_candidates": 10,
            # if your server has internet, you can map the same hospital_id to an image URL:
            # "cameras_by_hospital": { hospital_id: ["https://httpbin.org/image/jpeg"] }
        }
        sc, ct, data = post_json("/smart-nearby", smart_body)
        write_json(f, f"POST /smart-nearby  (status={sc})", data)

        f.write("\nDone. Check api_test_output.txt for all responses.\n")

if __name__ == "__main__":
    main()
