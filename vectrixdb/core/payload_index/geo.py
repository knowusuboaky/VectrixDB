"""
Geo Index

Spatial index for location fields using geohash-based grid.
Supports radius and bounding box queries.
"""

import math
import pickle
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .base import BasePayloadIndex


def encode_geohash(lat: float, lon: float, precision: int = 6) -> str:
    """
    Encode latitude/longitude to geohash string.

    Args:
        lat: Latitude (-90 to 90)
        lon: Longitude (-180 to 180)
        precision: Number of characters (default: 6, ~1.2km)

    Returns:
        Geohash string
    """
    BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"

    lat_range = [-90.0, 90.0]
    lon_range = [-180.0, 180.0]

    geohash = []
    bits = 0
    bit = 0
    ch = 0
    is_lon = True

    while len(geohash) < precision:
        if is_lon:
            mid = (lon_range[0] + lon_range[1]) / 2
            if lon >= mid:
                ch |= (1 << (4 - bit))
                lon_range[0] = mid
            else:
                lon_range[1] = mid
        else:
            mid = (lat_range[0] + lat_range[1]) / 2
            if lat >= mid:
                ch |= (1 << (4 - bit))
                lat_range[0] = mid
            else:
                lat_range[1] = mid

        is_lon = not is_lon
        bit += 1

        if bit == 5:
            geohash.append(BASE32[ch])
            bit = 0
            ch = 0

    return "".join(geohash)


def decode_geohash(geohash: str) -> Tuple[float, float]:
    """
    Decode geohash to latitude/longitude.

    Args:
        geohash: Geohash string

    Returns:
        Tuple of (latitude, longitude)
    """
    BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"

    lat_range = [-90.0, 90.0]
    lon_range = [-180.0, 180.0]

    is_lon = True

    for char in geohash:
        idx = BASE32.index(char.lower())

        for bit in range(4, -1, -1):
            if is_lon:
                mid = (lon_range[0] + lon_range[1]) / 2
                if idx & (1 << bit):
                    lon_range[0] = mid
                else:
                    lon_range[1] = mid
            else:
                mid = (lat_range[0] + lat_range[1]) / 2
                if idx & (1 << bit):
                    lat_range[0] = mid
                else:
                    lat_range[1] = mid

            is_lon = not is_lon

    lat = (lat_range[0] + lat_range[1]) / 2
    lon = (lon_range[0] + lon_range[1]) / 2

    return lat, lon


def get_geohash_neighbors(geohash: str) -> List[str]:
    """Get all 8 neighboring geohashes."""
    # Simplified: just get prefixes for a broader area
    if len(geohash) <= 1:
        return []

    return [geohash[:-1]]


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate distance between two points in kilometers.

    Args:
        lat1, lon1: First point
        lat2, lon2: Second point

    Returns:
        Distance in kilometers
    """
    R = 6371  # Earth's radius in km

    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)

    a = (math.sin(dlat / 2) ** 2 +
         math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return R * c


class GeoIndex(BasePayloadIndex):
    """
    Spatial index for geo-location fields using geohash-based grid.

    Efficiently finds documents within a radius or bounding box.

    Supports operators: geo_radius, geo_box

    Example:
        >>> index = GeoIndex("location")
        >>> index.add("doc1", {"lat": 40.7128, "lon": -74.0060})  # NYC
        >>> index.add("doc2", {"lat": 34.0522, "lon": -118.2437})  # LA
        >>> # Find within 100km of NYC
        >>> index.query("geo_radius", {"lat": 40.7, "lon": -74.0, "radius_km": 100})
    """

    def __init__(self, field_name: str, precision: int = 6):
        """
        Initialize geo index.

        Args:
            field_name: Name of the location field
            precision: Geohash precision (default: 6, ~1.2km cells)
        """
        super().__init__(field_name)
        self.precision = precision

        # Geohash grid index: geohash -> set of doc_ids
        self._geohash_index: Dict[str, Set[str]] = {}

        # Forward index: doc_id -> (lat, lon)
        self._doc_to_location: Dict[str, Tuple[float, float]] = {}

    @property
    def index_type(self) -> str:
        return "geo"

    def _parse_location(self, value: Any) -> Optional[Tuple[float, float]]:
        """Parse location from various formats."""
        if value is None:
            return None

        if isinstance(value, dict):
            lat = value.get("lat") or value.get("latitude")
            lon = value.get("lon") or value.get("lng") or value.get("longitude")
            if lat is not None and lon is not None:
                return (float(lat), float(lon))

        if isinstance(value, (list, tuple)) and len(value) >= 2:
            return (float(value[0]), float(value[1]))

        return None

    def add(self, doc_id: str, value: Any) -> None:
        """Add a location to the index."""
        location = self._parse_location(value)

        if location is None:
            return

        lat, lon = location

        # Validate coordinates
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return

        # Remove if already exists
        if doc_id in self._doc_to_location:
            self.remove(doc_id)

        # Compute geohash
        geohash = encode_geohash(lat, lon, self.precision)

        # Add to geohash index
        if geohash not in self._geohash_index:
            self._geohash_index[geohash] = set()
        self._geohash_index[geohash].add(doc_id)

        # Add to forward index
        self._doc_to_location[doc_id] = (lat, lon)
        self._count += 1

    def remove(self, doc_id: str) -> bool:
        """Remove document from index."""
        if doc_id not in self._doc_to_location:
            return False

        lat, lon = self._doc_to_location[doc_id]
        geohash = encode_geohash(lat, lon, self.precision)

        # Remove from geohash index
        if geohash in self._geohash_index:
            self._geohash_index[geohash].discard(doc_id)
            if not self._geohash_index[geohash]:
                del self._geohash_index[geohash]

        del self._doc_to_location[doc_id]
        self._count -= 1
        return True

    def update(self, doc_id: str, value: Any) -> None:
        """Update indexed location."""
        self.remove(doc_id)
        self.add(doc_id, value)

    def query(self, operator: str, value: Any) -> Set[str]:
        """
        Query index with given operator.

        Args:
            operator: geo_radius, geo_box
            value: Query parameters

        Returns:
            Set of matching document IDs
        """
        if operator == "geo_radius":
            return self._query_radius(value)
        elif operator == "geo_box":
            return self._query_box(value)
        else:
            raise ValueError(f"Unknown operator: {operator}")

    def _query_radius(self, params: Dict[str, Any]) -> Set[str]:
        """
        Find documents within radius of a point.

        Args:
            params: {"lat": float, "lon": float, "radius_km": float}

        Returns:
            Set of matching document IDs
        """
        center_lat = params.get("lat")
        center_lon = params.get("lon")
        radius_km = params.get("radius_km") or params.get("radius")

        if center_lat is None or center_lon is None or radius_km is None:
            raise ValueError("geo_radius requires lat, lon, and radius_km")

        center_lat = float(center_lat)
        center_lon = float(center_lon)
        radius_km = float(radius_km)

        # Get center geohash
        center_geohash = encode_geohash(center_lat, center_lon, self.precision)

        # Determine which geohashes to check based on radius
        # For simplicity, check all geohashes with matching prefix
        prefix_len = max(1, self.precision - int(math.log10(radius_km + 1)) - 1)
        prefix = center_geohash[:prefix_len]

        # Get candidate documents from matching geohashes
        candidates = set()
        for geohash, doc_ids in self._geohash_index.items():
            if geohash.startswith(prefix):
                candidates |= doc_ids

        # Filter by exact distance
        result = set()
        for doc_id in candidates:
            lat, lon = self._doc_to_location[doc_id]
            distance = haversine_distance(center_lat, center_lon, lat, lon)
            if distance <= radius_km:
                result.add(doc_id)

        return result

    def _query_box(self, params: Dict[str, Any]) -> Set[str]:
        """
        Find documents within a bounding box.

        Args:
            params: {"min_lat": float, "max_lat": float, "min_lon": float, "max_lon": float}
                    or {"top_left": {"lat": float, "lon": float}, "bottom_right": {"lat": float, "lon": float}}

        Returns:
            Set of matching document IDs
        """
        # Parse bounding box
        if "min_lat" in params:
            min_lat = float(params["min_lat"])
            max_lat = float(params["max_lat"])
            min_lon = float(params["min_lon"])
            max_lon = float(params["max_lon"])
        elif "top_left" in params:
            top_left = params["top_left"]
            bottom_right = params["bottom_right"]
            max_lat = float(top_left.get("lat"))
            min_lon = float(top_left.get("lon"))
            min_lat = float(bottom_right.get("lat"))
            max_lon = float(bottom_right.get("lon"))
        else:
            raise ValueError("geo_box requires bounding box coordinates")

        # Simple scan of all locations
        # (For better performance with large datasets, would need more sophisticated geohash range queries)
        result = set()
        for doc_id, (lat, lon) in self._doc_to_location.items():
            if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
                result.add(doc_id)

        return result

    def get_location(self, doc_id: str) -> Optional[Tuple[float, float]]:
        """Get location for a document."""
        return self._doc_to_location.get(doc_id)

    def get_stats(self) -> Dict[str, Any]:
        """Get index statistics."""
        stats = super().get_stats()
        stats.update({
            "precision": self.precision,
            "unique_geohashes": len(self._geohash_index),
        })

        if self._doc_to_location:
            lats = [loc[0] for loc in self._doc_to_location.values()]
            lons = [loc[1] for loc in self._doc_to_location.values()]
            stats.update({
                "lat_range": (min(lats), max(lats)),
                "lon_range": (min(lons), max(lons)),
            })

        return stats

    def save(self, path: Path) -> None:
        """Save index to disk."""
        path = Path(path)
        data = {
            "field_name": self.field_name,
            "precision": self.precision,
            "geohash_index": {k: list(v) for k, v in self._geohash_index.items()},
            "doc_to_location": self._doc_to_location,
            "count": self._count,
        }
        with open(path, "wb") as f:
            pickle.dump(data, f)

    def load(self, path: Path) -> "GeoIndex":
        """Load index from disk."""
        path = Path(path)
        with open(path, "rb") as f:
            data = pickle.load(f)

        self.field_name = data["field_name"]
        self.precision = data["precision"]
        self._geohash_index = {k: set(v) for k, v in data["geohash_index"].items()}
        self._doc_to_location = data["doc_to_location"]
        self._count = data["count"]

        return self
