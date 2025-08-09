import os, requests, random, base64
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Load .env here too (belt & suspenders)
load_dotenv()

from .wait_time import (
    get_wait_for_hospital,
    set_wait_for_hospital,
    _count_people_from_image_b64,
    is_openai_ready,
)

router = APIRouter(prefix="/nearby-hospitals", tags=["nearby-hospitals"])

# ----------------------- Config -----------------------
RADIUS_M = 10_000  # 10 km
API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
if not API_KEY:
    raise RuntimeError("Set GOOGLE_MAPS_API_KEY in .env")

# RNG fallback (for non-camera hospitals)
RNG_MIN_PEOPLE = 20
RNG_MAX_PEOPLE = 40
RNG_PER_PERSON_MIN = 8
RNG_PER_PERSON_MAX = 15
ENABLE_MOCK_RNG = os.getenv("MOCK_RNG_FOR_UNCOVERED", "1") != "0"

# Camera integration for the list view:
# - If PRIMARY_CAMERA_ID is set, we will use that exact hospital.
# - Else, we will use the *first* hospital after sorting by ETA.
# Provide the webcam URLs (semicolon-separated) in PRIMARY_CAMERA_URLS.
PRIMARY_CAMERA_ID = os.getenv("PRIMARY_CAMERA_ID", "").strip()  # optional: a specific Google Places ID
PRIMARY_CAMERA_URLS = [u.strip() for u in os.getenv("PRIMARY_CAMERA_URLS", "").split(";") if u.strip()]
PER_PERSON_FOR_CAMERA = int(os.getenv("PER_PERSON_FOR_CAMERA", "10"))  # minutes per person for camera hospital

class Query(BaseModel):
    lat: float = Field(..., description="Latitude")
    lng: float = Field(..., description="Longitude")
    max_results: int = Field(20, ge=1, le=20)

# ----------------------- Google APIs -----------------------
def _places_nearby_hospitals(lat: float, lng: float, *, max_results: int = 20) -> List[Dict[str, Any]]:
    url = "https://places.googleapis.com/v1/places:searchNearby"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": "places.id,places.displayName,places.location,places.googleMapsUri,places.name"
    }
    body = {
        "includedTypes": ["hospital"],
        "maxResultCount": max_results,
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": float(RADIUS_M)
            }
        }
    }
    r = requests.post(url, headers=headers, json=body, timeout=12)
    if not r.ok:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    data = r.json()
    out = []
    for p in data.get("places", []):
        loc = p.get("location", {})
        name = (p.get("displayName") or {}).get("text")
        maps_url = p.get("googleMapsUri")
        pid = p.get("id") or (p.get("name") or "").split("/", 1)[-1]
        if not name or "latitude" not in loc or "longitude" not in loc:
            continue
        out.append({
            "id": pid,
            "name": name,
            "lat": loc["latitude"],
            "lng": loc["longitude"],
            "maps_url": maps_url,
        })
    return out

def _route_matrix(origin_lat: float, origin_lng: float, dests: List[Dict[str, Any]]):
    if not dests:
        return []
    url = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": "originIndex,destinationIndex,status,condition,distanceMeters,duration"
    }
    body = {
        "origins": [
            {"waypoint": {"location": {"latLng": {"latitude": origin_lat, "longitude": origin_lng}}}}
        ],
        "destinations": [
            {"waypoint": {"location": {"latLng": {"latitude": d["lat"], "longitude": d["lng"]}}}}
            for d in dests
        ],
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE_OPTIMAL"
    }
    r = requests.post(url, headers=headers, json=body, timeout=12)
    if not r.ok:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()

def _parse_duration_seconds(val: str) -> Optional[float]:
    if not isinstance(val, str):
        return None
    if val.endswith("s"):
        try:
            return float(val[:-1])
        except Exception:
            return None
    return None

# ----------------------- Camera helpers -----------------------
def _fetch_camera_bytes(url: str, timeout: float = 6.0) -> Optional[bytes]:
    try:
        r = requests.get(url, timeout=timeout)
        if not r.ok:
            return None
        data = r.content or b""
        if len(data) < 100:  # tiny responses likely errors
            return None
        return data
    except Exception:
        return None

def _capture_and_count(camera_urls: List[str]) -> Tuple[int, List[Dict[str, Any]]]:
    """
    Pull fresh frames from given camera URLs and count with OpenAI.
    Requires OPENAI to be configured (strict). Returns (people_total, per-camera details).
    """
    details = []
    total = 0
    if not camera_urls:
        return 0, details
    if not is_openai_ready():
        # Strict behavior: if OpenAI isn't ready, raise
        raise RuntimeError("OpenAI vision not configured")

    for i, u in enumerate(camera_urls):
        cam_id = f"cam-{i+1}"
        img = _fetch_camera_bytes(u)
        if not img:
            details.append({"camera_id": cam_id, "people": 0, "status": "no_image"})
            continue
        b64 = base64.b64encode(img).decode("ascii")
        n = _count_people_from_image_b64(b64, require_openai=True)  # strict OpenAI
        total += n
        details.append({"camera_id": cam_id, "people": n, "status": "ok", "engine": "openai"})
    return total, details

# ----------------------- Endpoint -----------------------
@router.post("", summary="Find nearby hospitals + ETA (first hospital uses live camera)")
def nearby(q: Query):
    places = _places_nearby_hospitals(q.lat, q.lng, max_results=q.max_results)

    stats: Dict[int, Any] = {}
    matrix = _route_matrix(q.lat, q.lng, places)
    for row in matrix:
        if row.get("status", {}).get("code"):
            continue
        if row.get("condition") != "ROUTE_EXISTS":
            continue
        di = row["destinationIndex"]
        dist_km = round(row["distanceMeters"] / 1000.0, 2) if "distanceMeters" in row else None
        eta_min = round(_parse_duration_seconds(row["duration"]) / 60.0, 1) if "duration" in row else None
        stats[di] = (dist_km, eta_min)

    items = []
    for i, p in enumerate(places):
        hospital_id = p.get("id") or p.get("name")
        if isinstance(hospital_id, str) and hospital_id.startswith("places/"):
            hospital_id = hospital_id.split("/", 1)[1]

        dist_km, eta_min = stats.get(i, (None, None))
        row = {
            "hospital_id": hospital_id,
            "hospital_name": p["name"],
            "google_maps_location_link": p["maps_url"],
            "distance_km": dist_km,
            "eta_minutes": eta_min
        }

        # Attach any cached wait if exists
        wt = get_wait_for_hospital(hospital_id)
        if wt:
            row["current_people"] = wt["people"]
            row["current_estimated_wait_minutes"] = wt["estimated_wait_minutes"]
            row["wait_last_updated"] = wt["ts"]

        items.append(row)

    # Sort by ETA then distance
    items.sort(key=lambda x: (x["eta_minutes"] is None, x["eta_minutes"], x["distance_km"]))

    # ---------------- FIRST HOSPITAL USES CAMERA ----------------
    # Choose the target hospital: explicit ID > first in list
    target_idx = None
    if PRIMARY_CAMERA_ID:
        for idx, it in enumerate(items):
            if it["hospital_id"] == PRIMARY_CAMERA_ID:
                target_idx = idx
                break
    if target_idx is None and items:
        target_idx = 0

    # If camera URLs are provided, capture NOW and override fields for the target hospital
    if target_idx is not None and PRIMARY_CAMERA_URLS:
        try:
            people, cams = _capture_and_count(PRIMARY_CAMERA_URLS)
            items[target_idx]["current_people"] = people
            items[target_idx]["current_estimated_wait_minutes"] = int(people * PER_PERSON_FOR_CAMERA)
            items[target_idx]["wait_last_updated"] = datetime.now(timezone.utc).isoformat()
            items[target_idx]["_camera"] = {"used": True, "cameras": cams}  # for debug; your UI can ignore
        except Exception as e:
            # If camera/OpenAI fails, keep going; you’ll still get RNG/cached values below for others
            items[target_idx]["_camera"] = {"used": False, "error": str(e)}

    # ---------------- RNG for others (only if no wait yet) ----------------
    for idx, row in enumerate(items):
        if idx == target_idx:
            # We already set live camera result (or left as-is if error)
            continue
        if "current_people" in row and row.get("current_people") is not None:
            continue  # already has wait (cached from pushes)
        if not ENABLE_MOCK_RNG:
            continue
        rng_people = random.randint(RNG_MIN_PEOPLE, RNG_MAX_PEOPLE)
        per_person = random.randint(RNG_PER_PERSON_MIN, RNG_PER_PERSON_MAX)
        est = int(rng_people * per_person)
        # Persisting RNG to in-memory cache makes subsequent calls consistent for demo
        set_wait_for_hospital(row["hospital_id"], {
            "hospital_id": row["hospital_id"],
            "people": rng_people,
            "per_person_minutes": per_person,
            "estimated_wait_minutes": est,
            "cameras": [{"camera_id": "rng", "people": rng_people}],
        })
        wt = get_wait_for_hospital(row["hospital_id"])
        row["current_people"] = wt["people"]
        row["current_estimated_wait_minutes"] = wt["estimated_wait_minutes"]
        row["wait_last_updated"] = wt["ts"]

    return {"count": len(items), "hospitals": items}
