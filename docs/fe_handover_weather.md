# FE Handover: Weather Data on Snapshots

## Summary

Each timelapse snapshot now has weather data attached at the time of capture. The backend fetches current weather conditions from OpenWeather when a snapshot is ingested and stores it alongside the IMG# record in DynamoDB. The snapshot API responses now include a `weather` field.

---

## How It Works (BE)

1. Camera POSTs a JPEG to `POST /v1/ingest/{token}`
2. Ingest handler looks up the site's `latitude`/`longitude` from DynamoDB
3. Calls OpenWeather Current Weather API (3-second timeout, fail-open)
4. Stores the weather as a nested map on the IMG# DynamoDB record
5. Snapshot API responses include the weather when present

If the weather call fails (network error, bad API key, missing lat/lng), the snapshot still saves — weather is just `null`.

---

## API Response Changes

### GET /v1/snapshots/latest

Both single-camera and all-cameras modes now include `weather`:

**Single camera** (`?site_id=X&camera_id=Y`):
```json
{
  "camera_id": "cam_01",
  "timestamp": "2026-06-10T14:00:00Z",
  "key": "red_construction/red_wsm/cam_01/2026/06/10/2026-06-10T14:00:00Z.jpg",
  "presigned_url": "https://...",
  "expires_in": 300,
  "age_seconds": 142,
  "weather": {
    "condition": "Clouds",
    "description": "overcast clouds",
    "temp_c": 14.2,
    "feels_like_c": 12.8,
    "humidity_pct": 72,
    "wind_speed_ms": 3.4,
    "wind_deg": 220,
    "visibility_m": 10000,
    "cloud_pct": 90
  }
}
```

**All cameras** (no `camera_id`):
```json
{
  "cameras": [
    {
      "camera_id": "cam_01",
      "camera_name": "Front Gate",
      "timestamp": "2026-06-10T14:00:00Z",
      "presigned_url": "https://...",
      "expires_in": 300,
      "age_seconds": 142,
      "weather": { ... }
    }
  ]
}
```

### GET /v1/snapshots (paginated list)

Each image in the `images` array now optionally includes `weather`:

```json
{
  "images": [
    {
      "timestamp": "2026-06-10T14:00:00Z",
      "camera_id": "cam_01",
      "key": "...",
      "presigned_url": "https://...",
      "expires_in": 300,
      "weather": {
        "condition": "Rain",
        "description": "light rain",
        "temp_c": 11.5,
        "feels_like_c": 9.2,
        "humidity_pct": 88,
        "wind_speed_ms": 5.1,
        "wind_deg": 195,
        "visibility_m": 6000,
        "cloud_pct": 100
      }
    },
    {
      "timestamp": "2026-06-09T14:00:00Z",
      "camera_id": "cam_01",
      "key": "...",
      "presigned_url": "https://...",
      "expires_in": 300,
      "weather": null
    }
  ],
  "next_cursor": "...",
  "total_available": 51
}
```

---

## Weather Object Schema

| Field          | Type   | Description                              |
|----------------|--------|------------------------------------------|
| `condition`    | string | Main condition: "Clear", "Clouds", "Rain", "Snow", "Drizzle", "Thunderstorm", "Mist", "Fog" etc. |
| `description`  | string | Detailed description: "light rain", "overcast clouds", "clear sky" |
| `temp_c`       | number | Temperature in Celsius                   |
| `feels_like_c` | number | "Feels like" temperature in Celsius      |
| `humidity_pct` | number | Humidity percentage (0–100)              |
| `wind_speed_ms`| number | Wind speed in metres/second              |
| `wind_deg`     | number | Wind direction in degrees (0=N, 90=E, 180=S, 270=W) |
| `visibility_m` | number | Visibility in metres (max 10000)         |
| `cloud_pct`    | number | Cloud coverage percentage (0–100)        |

---

## Handling on the Frontend

### When `weather` is present

Display weather info alongside the snapshot. Suggested approaches:

- **Overlay badge** on the snapshot thumbnail (e.g. "14°C ☁️")
- **Sidebar/panel** when viewing a snapshot in detail
- **Gallery filter** — let users filter by weather condition (rainy days vs clear days)
- **Timelapse scrubber** — show weather as you scroll through the timeline

### When `weather` is null

This happens for:
- Snapshots taken before the weather feature was deployed
- Snapshots where the weather API was unreachable at ingest time
- Sites without lat/lng configured

Just don't show a weather badge. No error state needed.

### Suggested icons

Map the `condition` field to icons:

| condition      | Icon suggestion     |
|----------------|---------------------|
| Clear          | ☀️ sun              |
| Clouds         | ☁️ cloud            |
| Rain           | 🌧️ rain             |
| Drizzle        | 🌦️ light rain       |
| Thunderstorm   | ⛈️ thunderstorm      |
| Snow           | ❄️ snow             |
| Mist / Fog     | 🌫️ fog              |
| Haze / Smoke   | 🌫️ haze             |

### Wind direction

`wind_deg` is degrees from north (meteorological convention). To display as a compass arrow or cardinal direction:

```ts
function windDirection(deg: number): string {
  const dirs = ['N','NE','E','SE','S','SW','W','NW'];
  return dirs[Math.round(deg / 45) % 8];
}
```

---

## Backfill

Existing snapshots (taken before this feature) will have `weather: null`. There's no plan to backfill historical weather — it only applies to new ingests going forward.

---

## Dependencies

- The site must have `latitude` and `longitude` set (see the PATCH /v1/sites/{site_id} endpoint — separate handover doc).
- The `OPENWEATHER_API_KEY` environment variable must be configured on the ingest Lambda.
- Free tier OpenWeather key supports 1000 calls/day — more than enough for 15-min cadence across a few cameras.

---

## Files Changed (BE)

| File | What |
|------|------|
| `src/sitespy/weather.py` | New module — fetches weather from OpenWeather, converts to DynamoDB format |
| `src/sitespy/handlers/ingest.py` | Calls `fetch_current_weather()` during timelapse save path only |
| `src/sitespy/data.py` | `put_img_record()` now accepts optional `weather` map |
| `src/sitespy/handlers/snapshots.py` | Both list and latest endpoints now include `weather` in responses |
| `template.yaml` | Added `OpenWeatherApiKey` parameter + env var on IngestFunction |
