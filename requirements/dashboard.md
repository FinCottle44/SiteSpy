# Dashboard: Remote Timelapse & Image Browser

## 1. Objective

**SiteSpy** is a web-based portal for stakeholders to log in and browse the historical archive of hourly snapshots from pole-mounted Axis P1455-LE cameras. A site may host one or more cameras and the UI MUST accommodate that cleanly. The dashboard also hosts the flag-review console used by tenant admins and super admins to triage camera health issues, plus full user, tenant, and camera management for admins.

Visual direction, tokens, and components are defined in `design_system.md` — liquid-glass aesthetic on Inter, built on shadcn/ui with the approved palette. This document defines *what* screens exist and *what* they do; `design_system.md` defines *how* they look.

## 2. Tech Stack (Frontend — Kiro-Built)

- **Framework:** React 18 + TypeScript
- **Auth:** AWS Amplify (Cognito integration)
- **Styling:** Tailwind CSS + CSS variables for tokens (per `design_system.md`)
- **Component library:** shadcn/ui (Radix under the hood), restyled to SiteSpy tokens
- **Build:** Vite
- **Hosting:** AWS Amplify Hosting (region `eu-west-2`)
- **API Communication:** Axios with Bearer token from Amplify session; `X-Correlation-Id` header generated per request
- **Typeface:** Inter variable (self-hosted via `@fontsource-variable/inter`)
- **Region:** `eu-west-2` for UK data residency

## 3. Core Functional Requirements

### 3.1 Authentication
- The app MUST use Amplify to handle login/signup against the Cognito User Pool.
- Unauthenticated users MUST be redirected to the login page.
- On 403 from any API call, the UI MUST display "Access Denied: You do not have access to this site."
- After login, the frontend resolves the caller's role from `cognito:groups` (see `multi-tenant-auth.md` Section 1) and renders role-appropriate navigation.

### 3.2 Site View (Multi-Camera Hero)

When the user opens a site:
1. Call `GET /sites/{site_id}` to fetch the camera list.
2. Call `GET /snapshots/latest?site_id=X` (no `camera_id`) to fetch the latest image for every camera in one request.
3. Render the hero view:
   - **Single-camera site:** show the sole image at full width, mirroring the original hero experience.
   - **Multi-camera site:** show a responsive tile grid, one tile per camera, each labelled with `camera_name`. Each tile has its own heartbeat indicator derived from that camera's `age_seconds`. Clicking a tile enters the single-camera focused view.
4. If `age_seconds > 7200` (2 hours), the tile displays "Camera may be offline."
5. A **Flag this camera** button is present on every tile and on the single-camera focused view (see Section 7). Flags always target a specific camera.

### 3.3 Timeline / Gallery
- The gallery is always scoped to one camera. A camera selector (hidden on single-camera sites) sits at the top of the gallery view.
- Call `GET /snapshots?site_id=X&camera_id=Y&from=<date>&to=<date>` to fetch a paginated list.
- Display as a responsive grid of thumbnails (chronological, newest first).
- Provide a date picker to filter by day.
- Handle pagination via the `next_cursor` field.
- Gracefully handle gaps (missing hours) — show a placeholder or skip, do not show broken images.
- Each thumbnail MUST be selectable (checkbox overlay) for timelapse exclusion (see Section 6.2).

### 3.4 Lightbox
- Clicking a thumbnail opens a full-resolution view.
- Display metadata: timestamp (formatted to local timezone), site name, camera name.
- Arrow keys or swipe navigate between images of the same camera.

### 3.5 Status Monitor (Heartbeat)
- The persistent header indicator summarizes the worst camera in the currently viewed site.
- Green: all cameras < 90 minutes old.
- Yellow: any camera 90–180 minutes old.
- Red: any camera > 180 minutes old.
- Hovering reveals the per-camera breakdown.

## 4. Data Source

- **Storage:** AWS S3, accessed exclusively via pre-signed URLs returned by the API.
- **Key format:** `<tenant_id>/<site_id>/<camera_id>/<YYYY>/<MM>/<DD>/<YYYY-MM-DDTHH:mm:ssZ>.jpg`
- **The frontend MUST NOT have direct S3 access.** All image retrieval goes through the API Lambda which enforces tenant isolation.

## 5. Navigation & Selectors

Three levels of selection, rendered based on role:

- **Tenant picker** — super admin only.
- **Site picker:**
  - Regular user: lists sites in `custom:site_access`. Hidden when only one site is assigned.
  - Tenant admin: lists all sites in the tenant.
  - Super admin: lists all sites in the selected tenant.
- **Camera picker** — present on any view scoped to a single camera (gallery, lightbox, timelapse generator). Hidden on single-camera sites.

## 6. Timelapse Generator (Future — not MVP)

Scoped as a future feature. Documented here so the image-exclusion UX can be designed alongside the gallery.

### 6.1 Generator Flow
- User picks a site, a camera, a date range (`from` / `to`), and a frame rate. One timelapse = one camera.
- User optionally supplies an exclusion list (see 6.2).
- Submits via `POST /timelapses`. The dashboard polls `GET /timelapses/{id}` and offers a download link when the render completes.
- On a multi-camera site, the user can submit per-camera renders back-to-back; each is tracked independently.

### 6.2 Image Exclusion UX

Construction cameras can fall off their mounts. A timelapse that includes ground-level images after a fall is useless. The dashboard MUST offer **both** exclusion modes in the generator form:

1. **Range mode** — the user enters one or more `from`/`to` windows (datetime pickers). Every snapshot whose timestamp falls inside a window is skipped.
2. **Per-image mode** — the user ticks checkboxes on individual thumbnails in the gallery for the selected camera. Selected timestamps are passed as an explicit array.

Both modes can be combined in a single request. Each exclusion entry requires a short **reason** string (e.g., "camera fell off mount") — this is stored for audit purposes.

Exclusions are **one-shot**: they apply only to the render being submitted. They are NOT persisted on the site or camera and are NOT remembered between renders. If the user wants to exclude the same period again, they re-enter it. This keeps the data model simple and avoids stale, unmanaged exclusion lists.

Excluded images remain in S3 untouched. Exclusions only affect the FFmpeg render pipeline.

### 6.3 Visibility

The resulting MP4 is visible to:
- All users in the site (anyone with that `site_id` in `site_access`).
- All tenant admins in the tenant.

The exclusion audit (who excluded what, with reasons) is visible to tenant admins and super admins, primarily for dispute resolution in construction contexts.

## 7. Flag a Camera (All Users)

Any authenticated user who can view a site can raise a flag on any camera in that site. Flags always target a specific camera, never a whole site.

### 7.1 Raise-Flag UI
- **Entry points:** "Flag this camera" button on every camera tile in the hero view, on the single-camera focused view, and in the lightbox overlay.
- **Form fields:**
  - **Reason** (dropdown, required): Stale image, Physical damage, Obstruction, Image quality, Other.
  - **Note** (textarea): required when Reason = Other, otherwise optional. Max 1000 chars.
- The form pre-fills the camera name so the user always knows which camera they're flagging.
- On submit, calls `POST /flags` with `site_id` and `camera_id`. Shows a confirmation toast: "Flag raised. An admin has been notified."
- If the API returns an existing open flag (duplicate-suppression, same reason on same camera), the toast reads "This issue has already been flagged and is being reviewed."

### 7.2 My-Camera Flag Indicator
- On every camera tile and single-camera focused view, if an open or acknowledged flag exists for that specific camera, show a warning badge ("⚠ Flagged: Physical damage"). Clicking it shows the current flag details and status.
- Two cameras at the same site can carry independent flags — the badge always reflects only the camera it decorates.

## 8. Flag Review Console (Tenant Admins + Super Admins)

A dedicated route (`/flags`) that lists open and acknowledged flags.

### 8.1 Scope by Role
- **Tenant admin:** flags within their tenant only.
- **Super admin:** flags across all tenants, with a tenant filter dropdown and sort-by-age by default.

### 8.2 Flag Row UI

Each row shows:
- Tenant name (super admin view only).
- Site name, camera ID, and camera name.
- Reason + note.
- Source (user-raised vs. auto-raised).
- Raised-by (user name or "System").
- Raised-at (relative: "3 hours ago").
- Status badge.
- A thumbnail of the flagged camera's latest snapshot (from `latest_snapshot` in the flag response) — lets admins eyeball the issue without leaving the console.
- Action buttons: **Acknowledge**, **Resolve**, **Dismiss**. Each opens a modal for optional admin notes before calling `PATCH /flags/{flag_id}`.

### 8.3 Filters and Sort
- Filters: status (default: open + acknowledged), reason, site, camera, source (user vs. auto).
- Sort: newest first (default), oldest first, by reason.

### 8.4 Deep Link
Each flag row links to the site's gallery, with the camera selector pre-set to the flagged camera and scrolled to the timestamp of the latest snapshot at the time the flag was raised. One click takes the admin from the flag list straight to the offending image.

## 9. Admin Management UIs

### 9.1 User Management (Tenant Admin + Super Admin)

Route: `/admin/users`. Lists users within the caller's scope (tenant for tenant admins, all tenants for super admins).

- Search by email or name. Filter by role and site.
- Row actions: Edit name/role/site-access, Resend invitation, Delete (anonymize).
- "Invite user" button opens a modal with email, full name, role, and (for `user` role) a site-access multi-select sourced from `GET /v1/sites`. On submit, calls `POST /v1/users`.
- Site-access multi-select shows site names with a chip-style UI. Empty list is blocked on save for `user` role with a helpful message.

### 9.2 Tenant Management (Super Admin only)

Route: `/admin/tenants`.

- Table of all tenants with name, contact email, staleness threshold, camera count, open flag count, retention years.
- "Create tenant" modal collects tenant_id (validated against slug regex with live feedback), display name, primary contact email, and a required **CCTV notice acknowledgment checkbox** (stamps `cctv_notice_confirmed_at`).
- Edit modal updates display name, contact, retention years, staleness threshold.
- Soft-delete flow has a typed-confirmation step (user types the `tenant_id` to proceed) and surfaces the retention timeline so the admin knows when images will actually be purged.

### 9.3 Site & Camera Management (Tenant Admin + Super Admin)

Route: `/admin/sites`.

- Table of sites in scope. Columns: name, address, coordinates, cameras, open flags.
- "Create site" modal collects all fields in `POST /v1/sites`. Latitude/longitude can be entered manually or picked via a Leaflet map preview. Timezone defaults to `Europe/London`.
- Each site row expands to reveal its cameras with inline actions: add camera, rotate credentials, delete.
- **Camera credential display** is handled by a dedicated modal — not a table cell. On mint or rotate, the modal shows the username and password in monospace font with a single "Copy ingest config" button that puts the full Axis VAPIX recipient config on the clipboard. The modal warns that credentials will not be shown again. Closing the modal clears them from React state.

### 9.4 Profile & Session

Route: `/profile`.

- Display name, email, role, tenant, site access (read-only if role is `user`).
- Change password (routes through Cognito).
- Sign out of all sessions.

## 10. Error & Empty States

- **Network error:** toast with Retry button. Never a white screen.
- **403:** single full-page state with tenant/role context and a link back to the site list.
- **Empty site (no cameras yet):** helpful card with "Register a camera" CTA for admins, or "Ask your admin to register a camera" for users.
- **No snapshots in the selected range:** gallery shows a neutral empty state with the date picker highlighted.
- **Camera offline (stale image):** in-view banner with last-received timestamp and a "Raise flag" button.

## 11. Timezone Rendering

Every timestamp shown in the dashboard is rendered in the viewed site's timezone (from the site's `timezone` field), not in the viewer's browser timezone. See `software_logic.md` §12. All timestamps go through a shared `<Timestamp />` component — no raw `.toLocaleString()` calls.

## 12. Accessibility

WCAG 2.2 AA. Every interactive element has a visible focus ring, minimum 40×40px hit target, and a meaningful label. Status is never conveyed by color alone (icons + text labels everywhere). Keyboard navigation covers all admin flows — tested via Playwright. Screen reader testing with VoiceOver on macOS is part of release sign-off.
