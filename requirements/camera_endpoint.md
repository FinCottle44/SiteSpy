# Camera Ingest Endpoint — Wire Protocol

Behavioural spec for the server that receives JPEGs from Axis cameras. Captures the exact shape of requests observed from an Axis camera configured as an HTTP(S) recipient, so the real API can be built to match without a second round of trial-and-error.

This document complements `api_contract.md` (which defines the `POST /v1/ingest` contract at the API layer) by pinning down the HTTP-level details that the Lambda must handle.

---

## 1. Transport

- HTTP or HTTPS. Axis supports both; prefer HTTPS in production.
- Single endpoint per camera. The path can embed routing info as query params (the current contract uses `?cameraID=<camera_id>`).
- No webhooks, no polling. The camera pushes on its own schedule (hourly in our case).

---

## 2. Authentication — HTTP Basic, Challenge-Response

**Every camera gets its own unique credential pair.** See Section 6 for the rationale and provisioning flow.

### Wire behaviour

Axis does **not** send credentials preemptively. The flow is always:

1. Camera sends `POST` with the full image body and no `Authorization` header.
2. Server responds `401 Unauthorized` with `WWW-Authenticate: Basic realm="<anything>"`.
3. Camera immediately retries the same `POST` with `Authorization: Basic <base64(username:password)>`, body included.

This means **every successful upload costs two HTTP requests**. Both carry the full image payload. Budget bandwidth and Lambda invocations accordingly (API Gateway bills both, Lambda bills both).

### Server must

- Respond `401` with a `WWW-Authenticate: Basic realm="..."` header when `Authorization` is missing or invalid. Any realm string works; the camera doesn't inspect it.
- Validate credentials on the retry using a constant-time comparison.
- Not try to be clever about pre-authenticating or streaming the first request's body anywhere — it's discarded.

### Server must not

- Require Digest auth. Axis recipients support Basic only for outbound uploads.
- Require custom headers like `x-api-key` for auth. The Axis recipient UI has fields for URL, username, and password — that's it. Routing metadata (tenant, site, camera) must ride in the URL or be derivable from the credentials.
- Require cookies, CSRF tokens, or any interactive flow.

---

## 3. Request Shape (Authenticated Retry)

Observed from an Axis camera uploading a JPEG:

| Header | Example | Notes |
| :--- | :--- | :--- |
| `Host` | `192.168.1.214:5050` | Standard. |
| `Authorization` | `Basic Y2FtZXJhOmh1bnRlcjI=` | Base64 of `user:pass`. |
| `Accept` | `*/*` | |
| `Content-Type` | `image/jpeg` | **Raw JPEG, not multipart.** |
| `Content-Disposition` | `attachment; filename="image26-05-07_16-35-57-31_00001.jpg"` | Camera-generated filename. |
| `Content-Length` | `61945` | Byte length of the JPEG. |
| `Expect` | `100-continue` | Camera waits for `100 Continue` before sending the body. |

- **Method:** `POST`. Some Axis firmware uses `PUT`. Accept both at the same path.
- **Body:** the JPEG bytes directly. No multipart boundary, no form encoding, no wrapper.
- **Filename format observed:** `image<YY-MM-DD_HH-MM-SS-ff>_<counter>.jpg`. The timestamp is the camera's local time, the counter is an Axis-internal sequence.

---

## 4. Server Responsibilities

1. **Challenge** unauthenticated requests with `401 + WWW-Authenticate: Basic`.
2. **Validate** the credentials on the retry (constant-time compare against the per-camera secret).
3. **Handle `Expect: 100-continue` correctly.** The server must send `HTTP/1.1 100 Continue` before the camera will transmit the body. Any compliant WSGI/ASGI server handles this automatically when the body is actually read. API Gateway + Lambda handles it transparently.
4. **Read the raw body as bytes.** Do not expect `multipart/form-data` parsing to yield anything — there is no form. In Python: `request.get_data()` (Flask) or `await request.body()` (FastAPI). Read the body even on auth failure paths so the connection drains cleanly (otherwise the camera may retry oddly).
5. **Derive the filename** from the `Content-Disposition` header. Fall back to a server-generated timestamp + extension inferred from `Content-Type` if the header is missing or malformed.
6. **Persist the bytes** (S3 in production; filesystem in the local test server).
7. **Return `201 Created`** on success (or `200`; Axis accepts any 2xx). The response body is for your own debugging — Axis surfaces the status code in its event log but ignores the body.

### Additional methods to handle

- **`GET` and `HEAD`** on the same path: return `200 OK` with a trivial body. The "Test" button in the Axis recipient configuration probes the URL with `GET`. Anything non-2xx there shows the user a red error, even if `POST` uploads are working fine.
- **`PUT`**: treat as `POST`. Same body semantics.

---

## 5. What Does *Not* Apply

- **Digest auth on the ingest side.** Axis uses Digest for inbound (VAPIX, web UI); for outbound uploads it does whatever the recipient challenges for. Stick with Basic.
- **Multipart parsing.** The camera does not send `multipart/form-data` for this flow. Supporting it is only useful if you also want browsers / curl `-F` / generic clients to hit the same endpoint. Harmless to support, unnecessary for production.
- **The "Proxy" field** in the recipient configuration. That's for routing uploads through an outbound HTTP proxy (corporate egress). Unrelated to auth or headers.
- **TLS client certificates.** Axis can do them, but we don't require them — per-camera Basic credentials are the auth mechanism.
- **Custom headers from the camera.** The Axis recipient UI doesn't let you add arbitrary request headers. Anything the server needs must come from the URL, the credentials, or the body.

---

## 6. Per-Camera Credentials

**Every camera gets its own unique Basic auth credential pair.** Never a shared key, never a per-site key.

### Why per-camera

- **Revocation blast radius is one camera.** A leaked or suspected-leaked credential only affects that one device. Rotating one camera's secret does not disrupt any other camera on the same site.
- **Audit trail is unambiguous.** Every ingest log line ties directly to a specific `tenant_id + site_id + camera_id`. Shared credentials would force us to rely on the URL's `cameraID` param as the only identifier — and that param is user-editable on the camera side.
- **Rate limiting is per-credential.** API Gateway usage plans key off the credential; per-camera keys give us per-camera limits with no extra infrastructure (see `api_contract.md` — Rate Limits table).
- **Defence in depth.** The ingest Lambda cross-checks the submitted credential against the `tenant_id`, `site_id`, and `cameraID` in the request. A credential stolen from camera A cannot be used to upload under camera B's identity, even if an attacker rewrites the URL.

### Provisioning flow

Defined in `api_contract.md` under `POST /v1/sites/{site_id}/cameras`:

1. Tenant admin registers a camera via the dashboard.
2. The API generates a random username (e.g., `sitespy_cam_<12-hex>`) and a 32-char random password.
3. The pair is stored in AWS Secrets Manager at `sitespy/cameras/<tenant_id>/<site_id>/<camera_id>`.
4. The **response body returns the pair once** — this is the only time the password is retrievable. The dashboard displays a copy-paste block formatted for the Axis recipient config (URL, username, password, and the required `X-Tenant-ID` / `X-Site-ID` headers).
5. The installer pastes the credentials into the Axis camera's recipient configuration.

### Rotation

- `POST /v1/sites/{site_id}/cameras/{camera_id}/rotate-credentials` mints a new pair, overwrites the Secrets Manager entry, and returns the new pair once.
- The old credential is invalidated immediately. Any in-flight upload fails with `401` until the camera is reconfigured.
- Rotation is a normal operation (credential lost, installer churn, routine hygiene). It should not require engineering involvement.

### Storage rules

- Passwords are never stored in DynamoDB.
- Passwords are never logged. Log the username on auth failures; never the password, never the full `Authorization` header.
- Passwords are never returned by any `GET` endpoint. If the installer loses theirs, rotate.

---

## 7. Minimum Viable Endpoint (Pseudocode)

```
POST /v1/ingest?cameraID=<camera_id>
  if no Authorization header:
    return 401 with WWW-Authenticate: Basic realm="sitespy-ingest"

  username, password = parse_basic_auth(headers.Authorization)
  secret = secrets_manager.get("sitespy/cameras/<tenant>/<site>/<camera>")
  if not constant_time_equal(username, secret.username) or
     not constant_time_equal(password, secret.password):
    return 401

  # Defence in depth: credential must match the claimed routing
  if headers["X-Tenant-ID"] != secret.tenant_id or
     headers["X-Site-ID"]   != secret.site_id or
     query["cameraID"]      != secret.camera_id:
    return 401

  body = read_raw_body()          # raw JPEG, not multipart
  if not body:
    return 400

  filename = parse_content_disposition(headers) or timestamp_name(ext="jpg")
  s3.put_object(key=build_key(tenant, site, camera, now), body=body)
  write_image_record_to_dynamo(..., sha256=sha256(body))
  clear_stale_image_flag_if_open(camera_id)
  return 201 with {key, timestamp, camera_id, sha256}

GET|HEAD /v1/ingest?cameraID=...  → 200 OK  (for the "Test" button)
PUT  /v1/ingest?cameraID=...      → same as POST
```

---

## 8. Local Reference Implementation

A minimal Flask server matching this contract lives at `test_server/app.py`. It is intentionally permissive (accepts multipart too, static credentials hardcoded) for bench testing with a real camera on the bench. Not a production artifact.

Run it with:
```
python test_server/app.py
```
Creds are hardcoded at the top of the file; saved images land in `./images/`.
