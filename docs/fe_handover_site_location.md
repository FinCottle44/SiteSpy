# FE Handover: Edit Site Location (Lat/Lng/Timezone)

## Summary

The `PATCH /v1/sites/{site_id}` endpoint now supports updating a site's `latitude`, `longitude`, and `timezone` fields. This enables super admins (and tenant admins) to edit site location directly from the admin panel, which is important for the new weather-per-snapshot feature.

---

## API Contract

### Endpoint

```
PATCH /v1/sites/{site_id}?tenant_id=<tenant_id>
```

- **Auth:** Bearer token (Cognito JWT)
- **Roles:** `super_admin`, `tenant_admin`
- Super admins must pass `tenant_id` as a query parameter.
- Tenant admins have their `tenant_id` derived from the token.

### Request Body

All fields are optional — include only what you want to update. You can update any combination in a single request.

```json
{
  "latitude": 51.5074,
  "longitude": -0.1278,
  "timezone": "Europe/London",
  "ingest_hours": { "start": "07:00", "end": "18:00" }
}
```

### Field Validation

| Field       | Type   | Constraints                         |
|-------------|--------|-------------------------------------|
| `latitude`  | number | Must be between -90 and 90          |
| `longitude` | number | Must be between -180 and 180        |
| `timezone`  | string | Must be a valid IANA timezone ID    |

- `latitude` and `longitude` can be sent independently (you don't have to send both together).
- `timezone` is validated against IANA (e.g. `Europe/London`, `America/New_York`, `Australia/Sydney`).
- None of these fields accept `null` — they're required attributes of a site.

### Success Response (200)

```json
{
  "site_id": "red_wsm",
  "tenant_id": "red_construction",
  "latitude": 51.5074,
  "longitude": -0.1278,
  "timezone": "Europe/London"
}
```

Only the fields that were updated are echoed back (plus site_id and tenant_id).

### Error Responses

| Status | When                                           |
|--------|------------------------------------------------|
| 400    | Invalid lat/lng range, bad timezone, empty body |
| 403    | Caller lacks admin role or wrong tenant         |
| 404    | Site not found                                  |
| 500    | Internal error                                  |

Error body format:
```json
{
  "error": "BAD_REQUEST",
  "message": "latitude must be between -90 and 90."
}
```

---

## Reading Current Values

The existing `GET /v1/sites/{site_id}` response already includes these fields:

```json
{
  "site_id": "red_wsm",
  "site_name": "Red Construction - WSM",
  "tenant_id": "red_construction",
  "latitude": 51.5074,
  "longitude": -0.1278,
  "timezone": "Europe/London",
  "cameras": [...],
  "ingest_hours": { "start": "07:00", "end": "18:00" }
}
```

So the FE can pre-fill the form with current values from the GET response.

---

## Suggested UX

### Where

In the site detail / settings panel (admin view), add an editable "Location" section alongside the existing ingest hours config.

### What to Show

- **Latitude** — number input, 6 decimal places is enough (~0.1m precision)
- **Longitude** — number input, same precision
- **Timezone** — dropdown or searchable select of IANA timezones (or autocomplete from a list)
- A small embedded map preview showing the pin at the given coordinates (optional but nice)

### Behaviour

- Only show/enable for users with `super_admin` or `tenant_admin` role.
- On save, PATCH only the changed fields.
- Show a toast/notification on success.
- Display inline validation errors from the API (e.g. "latitude must be between -90 and 90").

### Why It Matters

The weather feature uses the site's lat/lng to fetch weather from OpenWeather at ingest time. If lat/lng is wrong or missing, no weather data gets attached to snapshots. Making this editable means admins can correct a site's location without needing a backend engineer.

---

## Example cURL

```bash
curl -X PATCH \
  "https://api.sitespy.io/v1/sites/red_wsm?tenant_id=red_construction" \
  -H "Authorization: Bearer <jwt>" \
  -H "Content-Type: application/json" \
  -d '{"latitude": 51.345, "longitude": -2.976, "timezone": "Europe/London"}'
```

---

## Files Changed (BE)

- `src/sitespy/handlers/sites_patch.py` — Added lat/lng/timezone validation and processing
- `src/sitespy/data.py` — New generic `update_site()` function that builds dynamic UpdateExpressions
- `src/sitespy/validation.py` — Already had `validate_latitude`, `validate_longitude`, `validate_timezone` (no changes needed)
- `src/sitespy/weather.py` — New module (uses lat/lng from site record at ingest time)

---

## Testing

The endpoint is deployed to dev. You can test with:

```bash
# Get current site to see existing lat/lng
curl "https://<dev-api>/v1/sites/main_site?tenant_id=red_construction" \
  -H "Authorization: Bearer <jwt>"

# Update location
curl -X PATCH \
  "https://<dev-api>/v1/sites/main_site?tenant_id=red_construction" \
  -H "Authorization: Bearer <jwt>" \
  -H "Content-Type: application/json" \
  -d '{"latitude": 51.345, "longitude": -2.976}'
```
