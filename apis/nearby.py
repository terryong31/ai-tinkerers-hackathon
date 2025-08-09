import os, requests
from typing import List, Dict, Any
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
if not API_KEY:
    raise RuntimeError("Set GOOGLE_MAPS_API_KEY in .env")

router = APIRouter(prefix="/nearby-hospitals", tags=["nearby-hospitals"])

RADIUS_M = 10_000  # fixed 10km

class Query(BaseModel):
    lat: float = Field(..., description="Latitude")
    lng: float = Field(..., description="Longitude")

def _places_nearby_hospitals(lat: float, lng: float) -> List[Dict[str, Any]]:
    """Google Places API (New, v1) Nearby Search for hospitals within 10km."""
    url = "https://places.googleapis.com/v1/places:searchNearby"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        # Field mask REQUIRED in v1; request only what we need
        "X-Goog-FieldMask": "places.displayName,places.location,places.googleMapsUri"
    }
    body = {
        "includedTypes": ["hospital"],
        "maxResultCount": 20,  # per-page cap
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
    out = []
    for i, p in enumerate(r.json().get("places", [])):
        loc = p.get("location") or {}
        plat, plng = loc.get("latitude"), loc.get("longitude")
        if plat is None or plng is None:
            continue
        out.append({
            "idx": i,  # keep index to match distance matrix results
            "name": (p.get("displayName") or {}).get("text"),
            "lat": plat,
            "lng": plng,
            "maps_url": p.get("googleMapsUri"),
        })
    return out

def _route_matrix(origin_lat: float, origin_lng: float, dests: List[Dict[str, Any]]):
    """Routes API Distance Matrix v2 (traffic-aware driving)."""
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
    r = requests.post(url, headers=headers, json=body, timeout=20)
    if not r.ok:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()  # JSON array of results

def _parse_duration_seconds(s: str) -> float:
    # "123s" or "123.45s" -> seconds
    return float(s.rstrip("s"))

@router.post("", summary="Hospitals within 10km, with name, Google Maps link, distance, ETA")
def nearby_hospitals(q: Query):
    places = _places_nearby_hospitals(q.lat, q.lng)
    matrix = _route_matrix(q.lat, q.lng, places)

    # destinationIndex -> (distance_km, eta_min)
    stats: Dict[int, Any] = {}
    for row in matrix:
        if row.get("status", {}).get("code"):              # skip errors
            continue
        if row.get("condition") != "ROUTE_EXISTS":
            continue
        di = row["destinationIndex"]
        dist_km = round(row["distanceMeters"] / 1000.0, 2) if "distanceMeters" in row else None
        eta_min = round(_parse_duration_seconds(row["duration"]) / 60.0, 1) if "duration" in row else None
        stats[di] = (dist_km, eta_min)

    items = []
    for i, p in enumerate(places):
        if i in stats:
            dist_km, eta_min = stats[i]
            items.append({
                "hospital_name": p["name"],
                "google_maps_location_link": p["maps_url"],
                "distance_km": dist_km,
                "eta_minutes": eta_min
            })

    # Sort by ETA then distance
    items.sort(key=lambda x: (x["eta_minutes"] is None, x["eta_minutes"], x["distance_km"]))
    return {"count": len(items), "hospitals": items}