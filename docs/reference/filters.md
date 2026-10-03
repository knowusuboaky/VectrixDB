# Filters

`search(filter=...)` narrows results by metadata before ranking. Three forms are accepted; they can be mixed. Every operator on this page is exercised against a real collection in `tests/unit/test_filter_grammar.py`, so the page and the behaviour cannot drift apart silently.

The examples below run against this collection:

```python
from vectrixdb import Vectrix

db = Vectrix("shop")
db.add(
    ["laptop stand", "standing desk mat", "espresso machine", "pour-over kettle"],
    metadata=[
        {
            "cat": "office",
            "price": 51.25,
            "tags": ["desk", "metal"],
            "stock": 3,
            "added": "2026-03-01",
            "dims": {"w": 20},
        },
        {
            "cat": "office",
            "price": 39.0,
            "tags": ["desk", "foam"],
            "stock": 0,
            "added": "2026-06-15",
            "dims": {"w": 90},
        },
        {
            "cat": "kitchen",
            "price": 420.0,
            "tags": ["coffee"],
            "stock": 1,
            "added": "2025-12-24",
            "dims": {"w": 30},
        },
        {
            "cat": "kitchen",
            "price": 65.0,
            "tags": ["coffee", "metal"],
            "stock": 12,
            "added": "2026-09-01",
            "dims": {"w": 25},
            "discontinued": True,
        },
    ],
)
```

## Simple form

A dict of field to value is equality. A dict of field to `{"$op": value}` is a comparison. Several operators on one field are ANDed.

```python
db.search("kettle", filter={"cat": "kitchen"})
db.search("kettle", filter={"price": {"$gte": 65, "$lte": 420}})
```

| Operator | Meaning | Example |
| --- | --- | --- |
| `$eq`, `$ne` | equal, not equal | `{"cat": {"$ne": "office"}}` |
| `$gt`, `$gte`, `$lt`, `$lte` | numeric or string comparison | `{"price": {"$lt": 60}}` |
| `$between` | inclusive range, `[low, high]` | `{"stock": {"$between": [1, 5]}}` |
| `$in`, `$nin` | value in, not in a list; against a list field, shares or shares no value | `{"cat": {"$in": ["kitchen", "bath"]}}` |
| `$any` | list field shares at least one value | `{"tags": {"$any": ["metal"]}}` |
| `$all` | list field contains every value | `{"tags": {"$all": ["desk", "metal"]}}` |
| `$contains`, `$icontains` | substring, case-sensitive or not | `{"title": {"$icontains": "stand"}}` |
| `$starts_with`, `$ends_with` | string prefix or suffix | `{"cat": {"$starts_with": "off"}}` |
| `$regex` | Python regular expression | `{"cat": {"$regex": "^kit"}}` |
| `$date_range` | inclusive `[start, end]` of ISO strings or datetimes | `{"added": {"$date_range": ["2026-01-01", "2026-06-30"]}}` |
| `$exists` | field present or absent | `{"discontinued": {"$exists": True}}` |
| `$is_null`, `$is_empty` | field is present and `None`; field is present and an empty string, list or dict | `{"notes": {"$is_empty": True}}` |
| `$geo_radius` | within a radius of a point | `{"loc": {"$geo_radius": {"center": {"lat": 43.6, "lon": -79.4}, "radius_km": 5}}}` |
| `$geo_box` | within a bounding box | `{"loc": {"$geo_box": {"min_lat": 43, "max_lat": 44, "min_lon": -80, "max_lon": -79}}}` |

### Absent, null and empty

These are three different states and each operator picks out exactly one.
A field that is not there is not null, and neither an absent field nor a null
one is empty:

| document | `$exists: True` | `$is_null: True` | `$is_empty: True` |
| --- | --- | --- | --- |
| `{"notes": "keep"}` | yes | no | no |
| `{"notes": None}` | yes | yes | no |
| `{"notes": ""}` | yes | no | yes |
| `{}` | no | no | no |

A field set to `None` takes part in equality and membership, so `{"notes":
None}` and `{"notes": {"$ne": "keep"}}` both match it. It takes no part in
ordering: `$gt` and its siblings are false against a null.

A field that is absent matches no comparison operator at all, `$ne` and
`$nin` included. Use `$exists` to ask about absence; it is the only operator
that can see it.

### Nested keys

Dotted paths reach into nested dicts and list positions: `{"dims.w": {"$gt": 28}}`, `{"authors.0": "Curie"}`.

### Dates

Store dates as ISO 8601 strings (`"2026-06-15"` or `"2026-06-15T09:30:00+00:00"`). The comparison operators then order them correctly as strings, and `$date_range` parses both sides. Mixing formats in one field defeats string ordering; pick one.

## Composition

```python
{"$and": [{"cat": "office"}, {"$or": [{"stock": 0}, {"price": {"$gt": 50}}]}]}
{"$not": {"cat": "office"}}
```

`$and`, `$or` and `$not` nest to any depth.

## Qdrant form

For code written against Qdrant, `must`, `should` and `must_not` are accepted with `match`, `range` and the other Qdrant condition keys:

```python
{
    "must": [{"key": "cat", "match": {"value": "kitchen"}}],
    "must_not": [{"key": "discontinued", "match": {"value": True}}],
}
```

## Extended form

A single condition can be spelled out, which is easier to build programmatically:

```python
{"field": "price", "op": "lte", "value": 51.25}
```

`op` is any operator name above without the `$`.

## Where filtering happens

Filters are applied to candidates as they come out of the index, so a very selective filter over a very large collection can return fewer than `limit` results even when more would match. Widen `limit` in that case; a pre-filtered index is on the roadmap.
