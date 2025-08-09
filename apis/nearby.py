import os, requests, random
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from .wait_time import get_wait_for_hospital, set_wait_for_hospital

load_dotenv()
API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
if not API_KEY:
    raise RuntimeError("Set GOOGLE_MAPS_API_KEY in .env")

router = APIRouter(prefix="/nearby-hospitals", tags=["nearby-hospitals"])
RADIUS_M = 10_000  # fixed 10km
# Demo RNG config
RNG_MIN_PEOPLE = 20
RNG_MAX_PEOPLE = 40
RNG_PER_PERSON_MIN = 8
RNG_PER_PERSON_MAX = 15
ENABLE_MOCK_RNG = os.getenv("MOCK_RNG_FOR_UNCOVERED", "1") != "0"

class Query(BaseModel):
    lat: float = Field(..., description="Latitude")
    lng: float = Field(..., description="Longitude")
    max_results: int = Field(20, ge=1, le=20)

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
        pid = (p.get("id") or (p.get("name") or "").split("/", 1)[-1])
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

@router.post("", summary="Find nearby hospitals + ETA", description="Uses Google Places (v1) and Routes Matrix (v2).")
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
        eta_min = None
        dur = row.get("duration")
        if isinstance(dur, str) and dur.endswith("s"):
            try:
                eta_min = round(float(dur[:-1]) / 60.0, 1)
            except Exception:
                pass
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

        wt = get_wait_for_hospital(hospital_id)

        # >>> DEMO RNG: if no live/cached wait, assign a mock headcount and store it
        if wt is None and ENABLE_MOCK_RNG:
            rng_people = random.randint(RNG_MIN_PEOPLE, RNG_MAX_PEOPLE)
            per_person = random.randint(RNG_PER_PERSON_MIN, RNG_PER_PERSON_MAX)
            est = int(rng_people * per_person)
            mock = {
                "hospital_id": hospital_id,
                "people": rng_people,
                "per_person_minutes": per_person,
                "estimated_wait_minutes": est,
                "cameras": [{"camera_id": "rng", "people": rng_people}],
            }
            set_wait_for_hospital(hospital_id, mock)
            wt = get_wait_for_hospital(hospital_id)
        # <<< DEMO RNG

        if wt:
            row["current_people"] = wt["people"]
            row["current_estimated_wait_minutes"] = wt["estimated_wait_minutes"]
            row["wait_last_updated"] = wt["ts"]

        items.append(row)

    items.sort(key=lambda x: (x["eta_minutes"] is None, x["eta_minutes"], x["distance_km"]))
    return {"count": len(items), "hospitals": items}