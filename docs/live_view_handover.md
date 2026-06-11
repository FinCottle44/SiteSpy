# SiteSpy — Live View Session Handover for Frontend Developers

This document covers the Live View Session feature. It follows the same style as `docs/api_handover.md` — everything you need to integrate the live feed into the dashboard is here.

## Overview

Live View lets an authenticated user start a 10-minute high-frequency feed for a specific camera. While the session is active, the camera pushes a new image every ~1 minute. The frontend polls a GET endpoint every 15 seconds to retrieve the latest live image and display a countdown to session expiry.

**Key facts:**
- Sessions last exactly 10 minutes from creation
- Only one session per camera at a time
- Images are served via presigned S3 URLs (valid 300 seconds)
- Cleanup is automatic — no user action needed after expiry

---

## Base URL

Same as the main API:

```
VITE_API_ENDPOINT=https://xxxxxxxxxx.execute-api.eu-west-2.amazonaws.com/prod
```

All routes require the same `Authorization: Bearer <cognito_id_token>` header and `X-Correlation-Id` header as the rest of the API.

---

## Endpoints

### POST /v1/sites/{site_id}/cameras/{camera_id}/live-session

Starts a new 10-minute live view session for the specified camera.

**Path params:**
- `site_id` — the site containing the camera
- `camera_id` — the camera to start a live session for

**Query params:**
- `tenant_id` (required for super admins only)

**No request body.**

**Response — session created (201):**
```json
{
  "session_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "expires_at": "2025-06-15T14:10:00Z",
  "camera_id": "cam_01"
}
```

**Response — session already active (409):**
```json
{
  "error": "SESSION_ALREADY_ACTIVE",
  "message": "A live session is already active for this camera."
}
```

**Key fields:**
- `session_id` — unique identifier for this session (UUID v4)
- `expires_at` — ISO 8601 UTC timestamp when the session will expire (always 10 minutes from creation)
- `camera_id` — echoed back for confirmation

**Usage:**
1. Call this endpoint when the user clicks "Start Live View"
2. Store the `expires_at` value to drive your countdown timer
3. Immediately begin polling GET (see below)

---

### GET /v1/sites/{site_id}/cameras/{camera_id}/live-session

Polls the session status and returns the latest live image. Call this every 15 seconds.

**Path params:**
- `site_id` — the site containing the camera
- `camera_id` — the camera to poll

**Query params:**
- `tenant_id` (required for super admins only)

**No request body.**

**Response — active session with live image (200):**
```json
{
  "status": "active",
  "session_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "expires_at": "2025-06-15T14:10:00Z",
  "latest_image": {
    "presigned_url": "https://s3.eu-west-2.amazonaws.com/sitespy-prod-snapshots/live/acme/site_01/cam_01/2025-06-15T14:04:00Z.jpg?X-Amz-...",
    "captured_at": "2025-06-15T14:04:00Z",
    "expires_in": 300
  }
}
```

**Response — active session, no image yet (200):**
```json
{
  "status": "active",
  "session_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "expires_at": "2025-06-15T14:10:00Z",
  "latest_image": null
}
```

**Response — no active session (200):**
```json
{
  "status": "none"
}
```

**Key fields:**
- `status` — either `"active"` or `"none"`
- `latest_image` — the most recent live snapshot, or `null` if no image has arrived yet
- `presigned_url` — temporary S3 URL for the live image (valid 300 seconds)
- `captured_at` — when the camera captured this image (ISO 8601 UTC)
- `expires_in` — always 300 (seconds until the presigned URL expires)

**Important:** The `status: "none"` response has no other fields. Use this as the signal to stop polling — the session has either expired naturally or been deleted.

---

### DELETE /v1/sites/{site_id}/cameras/{camera_id}/live-session

Ends a session before its natural expiry. Use this when the user clicks "Stop Live View".

**Path params:**
- `site_id` — the site containing the camera
- `camera_id` — the camera whose session to end

**Query params:**
- `tenant_id` (required for super admins only)

**No request body.**

**Response — session deleted (200):**
```json
{
  "status": "deleted"
}
```

**Response — no active session (404):**
```json
{
  "error": "NOT_FOUND",
  "message": "The requested resource was not found."
}
```

After a successful DELETE, subsequent GET requests will return `{"status": "none"}`. The user can start a new session immediately.

---

## Session Lifecycle

```
┌─────────────────────────────────────────────────────────────────┐
│  1. User clicks "Start Live View"                               │
│     → POST live-session → 201 { session_id, expires_at }        │
└───────────────────────────────┬─────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│  2. Session is ACTIVE (10-minute window)                        │
│     → Camera pushes every ~1 min, backend writes live images    │
│     → Frontend polls GET every 15 s                             │
│     → Display countdown: expires_at minus current time          │
│     → Display latest_image.presigned_url in <img>               │
└──────────────┬──────────────────────────────┬───────────────────┘
               │                              │
               ▼                              ▼
┌──────────────────────────┐   ┌──────────────────────────────────┐
│  3a. Natural expiry       │   │  3b. User clicks "Stop"          │
│  GET returns status:none  │   │  → DELETE live-session → 200     │
│  Stop polling             │   │  Stop polling                    │
└──────────────────────────┘   └──────────────────────────────────┘
               │                              │
               ▼                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  4. Cleanup (automatic, no frontend action needed)              │
│     → Live S3 objects expire via Lifecycle rule (~24 hours)     │
│     → DynamoDB records removed by TTL (~48 hours)               │
│     → User can start a new session immediately                  │
└─────────────────────────────────────────────────────────────────┘
```

**What to display at each stage:**

| Stage | UI State |
|---|---|
| Before session | "Start Live View" button enabled |
| POST returns 201 | Show countdown timer, begin polling, show loading spinner for image |
| GET returns `latest_image: null` | Keep spinner, show "Waiting for first image..." |
| GET returns `latest_image` with URL | Display image, update countdown |
| GET returns `status: "none"` | Show "Session ended", re-enable Start button |
| DELETE returns 200 | Show "Session stopped", re-enable Start button |
| POST returns 409 | Show toast "A live session is already active" |

---

## Polling Pattern

### Recommended: 15-second interval

Poll `GET /v1/sites/{site_id}/cameras/{camera_id}/live-session` every 15 seconds while the session is active.

**Why 15 seconds?**
- The camera pushes every ~1 minute
- 15 seconds gives a good balance between freshness and API cost
- At worst, you display an image that's 15 seconds stale (the image itself is max ~1 minute old)

### Countdown Display

Calculate the remaining time from `expires_at`:

```typescript
const remainingMs = new Date(expiresAt).getTime() - Date.now();
const remainingSec = Math.max(0, Math.floor(remainingMs / 1000));
const minutes = Math.floor(remainingSec / 60);
const seconds = remainingSec % 60;
// Display: "9:45 remaining"
```

Update the countdown every second (use `setInterval` at 1000ms). This is purely client-side — no API call needed.

### Handling `status: "none"`

When a poll returns `{"status": "none"}`, the session has ended (either natural expiry or deletion by another client). Stop polling immediately and update the UI.

### Handling `status: "active"`

Continue polling. Update the displayed image if `latest_image.captured_at` has changed since the last poll. If `latest_image` is `null`, the camera hasn't sent an image yet — keep showing the loading state.

---

## Presigned URL Handling

Live images use the same presigned URL pattern as the rest of SiteSpy.

### Lifecycle

| Event | Time |
|---|---|
| URL generated by GET response | T+0 |
| URL expires | T+300s (5 minutes) |
| Recommended refresh | T+240s (4 minutes — 60s before expiry) |

### Rules

1. Use `presigned_url` directly as `<img src="...">` — no additional auth needed
2. URLs expire after 300 seconds — do NOT cache them in state that persists across polls
3. If an image fails to load (403 from S3), the URL has expired — the next poll will return a fresh one
4. Each GET poll returns a fresh presigned URL for the latest image, so normal 15-second polling inherently refreshes URLs

### Refresh Strategy

Since you poll every 15 seconds, you naturally get a fresh URL on each poll. The 300-second expiry is generous — you'll never hit it under normal polling. However, if polling is paused (tab becomes inactive, network issues), and you resume:

1. Check if the current `presigned_url` is still valid: `Date.now() < pollTimestamp + (expires_in * 1000)`
2. If expired, immediately trigger a new GET poll before displaying the image
3. If the `<img>` element fires an `onerror` event (403 from S3), trigger a fresh poll

```typescript
// Handle image load failure (expired URL)
const handleImageError = () => {
  // URL expired — the next poll will provide a fresh one
  // Force an immediate poll instead of waiting for the interval
  pollNow();
};
```

---

## Error Codes

All errors follow the standard SiteSpy shape:

```json
{
  "error": "ERROR_KEY",
  "message": "Human-readable description."
}
```

| HTTP Code | Error Key | Meaning | Frontend Action |
|---|---|---|---|
| 400 | `BAD_REQUEST` | Missing/malformed path params or super admin missing `tenant_id` | Show validation error |
| 401 | `UNAUTHORIZED` | Token expired or missing | Redirect to login |
| 403 | `ACCESS_DENIED` | Valid token but no permission for this site | Show "Access Denied" |
| 404 | `NOT_FOUND` | Camera doesn't exist (POST), or no active session (DELETE) | Show "Not Found" state |
| 409 | `SESSION_ALREADY_ACTIVE` | A live session is already running for this camera | Show info toast, poll GET instead |
| 429 | `TOO_MANY_REQUESTS` | Rate limited | Retry with backoff |
| 500 | `INTERNAL_ERROR` | Server failure (DynamoDB error) | Show generic error with retry button |

### Handling 409 `SESSION_ALREADY_ACTIVE`

This is not necessarily an error from the user's perspective. It means someone (possibly the same user in another tab) already started a session. The recommended frontend behaviour:

1. Show an informational toast: "A live session is already active for this camera"
2. Switch to polling GET immediately — you can view the active session's images
3. Optionally show a "Stop Session" button so the user can end it

---

## TypeScript Polling Loop Example

A complete example showing the full session lifecycle:

```typescript
import api from './api/client'; // Your axios instance with auth interceptor

interface LiveImage {
  presigned_url: string;
  captured_at: string;
  expires_in: number;
}

interface LiveSessionState {
  status: 'active' | 'none';
  session_id?: string;
  expires_at?: string;
  latest_image?: LiveImage | null;
}

interface SessionController {
  stop: () => Promise<void>;
  cleanup: () => void;
}

async function startLiveSession(
  siteId: string,
  cameraId: string,
  onImageUpdate: (image: LiveImage | null) => void,
  onCountdownUpdate: (remainingSec: number) => void,
  onSessionEnd: (reason: 'expired' | 'deleted' | 'error') => void,
): Promise<SessionController> {
  // 1. Start the session
  const { data: session } = await api.post(
    `/v1/sites/${siteId}/cameras/${cameraId}/live-session`
  );

  const expiresAt = new Date(session.expires_at).getTime();

  // 2. Start countdown timer (updates every second)
  const countdownInterval = setInterval(() => {
    const remainingMs = expiresAt - Date.now();
    const remainingSec = Math.max(0, Math.floor(remainingMs / 1000));
    onCountdownUpdate(remainingSec);

    if (remainingSec <= 0) {
      cleanup();
      onSessionEnd('expired');
    }
  }, 1000);

  // 3. Start polling loop (every 15 seconds)
  let pollActive = true;

  const poll = async () => {
    if (!pollActive) return;

    try {
      const { data } = await api.get<LiveSessionState>(
        `/v1/sites/${siteId}/cameras/${cameraId}/live-session`
      );

      if (data.status === 'none') {
        cleanup();
        onSessionEnd('expired');
        return;
      }

      // Update image if available
      onImageUpdate(data.latest_image ?? null);
    } catch (err: any) {
      if (err.response?.status === 401) {
        cleanup();
        onSessionEnd('error');
        return;
      }
      // Other errors: log and continue polling
      console.warn('Live session poll failed:', err.message);
    }

    // Schedule next poll
    if (pollActive) {
      setTimeout(poll, 15_000);
    }
  };

  // Kick off first poll immediately
  poll();

  // 4. Cleanup function
  const cleanup = () => {
    pollActive = false;
    clearInterval(countdownInterval);
  };

  // 5. Return controller
  return {
    stop: async () => {
      try {
        await api.delete(
          `/v1/sites/${siteId}/cameras/${cameraId}/live-session`
        );
      } catch {
        // Session may have already expired — that's fine
      }
      cleanup();
      onSessionEnd('deleted');
    },
    cleanup,
  };
}
```

**Usage in a React component:**

```typescript
const [image, setImage] = useState<LiveImage | null>(null);
const [countdown, setCountdown] = useState<number>(600);
const [isActive, setIsActive] = useState(false);
const controllerRef = useRef<SessionController | null>(null);

const handleStart = async () => {
  try {
    controllerRef.current = await startLiveSession(
      siteId,
      cameraId,
      setImage,
      setCountdown,
      (reason) => {
        setIsActive(false);
        setImage(null);
        // Show appropriate message based on reason
      },
    );
    setIsActive(true);
  } catch (err: any) {
    if (err.response?.status === 409) {
      // Session already active — start polling instead
      toast.info('A live session is already active');
    } else {
      toast.error('Failed to start live session');
    }
  }
};

const handleStop = async () => {
  await controllerRef.current?.stop();
};

// Cleanup on unmount
useEffect(() => {
  return () => controllerRef.current?.cleanup();
}, []);
```

---

## Ingest Response Change: `live_captured`

The ingest endpoint (`POST /v1/ingest/{token}`) now includes a `live_captured` boolean in its responses. This field indicates whether a live snapshot was written during that push.

**This is primarily useful for backend monitoring and diagnostics.** Frontend developers consuming the dashboard API do not call the ingest endpoint directly (cameras do). However, if you're building admin tooling that monitors ingest health, here's what the field means:

### 201 Response (timelapse snapshot saved):

```json
{
  "key": "acme/site_01/cam_01/2025/06/15/2025-06-15T14:00:00Z.jpg",
  "timestamp": "2025-06-15T14:00:00Z",
  "camera_id": "cam_01",
  "sha256": "a1b2c3d4e5f6...",
  "size_bytes": 204800,
  "live_captured": true
}
```

- `live_captured: true` — a live session was active, so the image was also written to the live prefix
- `live_captured: false` — no active session, only the timelapse snapshot was written

### 200 Response (cadence-skip — timelapse not saved):

```json
{
  "status": "skipped",
  "reason": "cadence_filter",
  "camera_id": "cam_01",
  "live_captured": true
}
```

- `live_captured: true` — timelapse was skipped (< 15 min since last save) but a live session is active, so the image was written to the live prefix
- `live_captured: false` — timelapse was skipped and no live session is active; image was not stored anywhere

---

## Role-Based Access

The live session endpoints follow the same access model as the rest of the SiteSpy API:

| Role | Access |
|---|---|
| **User** | Can start/poll/stop sessions for cameras on sites in their `custom:site_access` list |
| **Tenant Admin** | Can start/poll/stop sessions for any camera in their tenant |
| **Super Admin** | Can start/poll/stop sessions for any camera in any tenant (must pass `?tenant_id=`) |

Super admins must always include `?tenant_id=<id>` as a query parameter on all three endpoints. Omitting it returns 400 `BAD_REQUEST`.

---

## CORS & Headers

Same as the main API:
- Methods: `GET, POST, DELETE, OPTIONS`
- Headers: `Content-Type, Authorization, X-Correlation-Id`
- Origin: `*`

No special CORS handling needed.

---

## What the API Does NOT Provide

- **WebSocket/SSE push** — the backend does not push images to the frontend. You must poll.
- **Multiple simultaneous sessions per camera** — only one session can be active per camera at a time.
- **Session extension** — sessions cannot be extended. Start a new one after expiry.
- **Historical live images** — live images are cleaned up automatically. They are not available in the snapshots list endpoint.
