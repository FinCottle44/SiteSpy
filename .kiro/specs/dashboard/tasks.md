# Implementation Plan: Dashboard (Phase 0 MVP)

## Overview

Build the SiteSpy web dashboard: a React 18 + TypeScript SPA where authenticated users browse camera snapshots, monitor camera health, and raise flags. The implementation covers the frontend app (Vite + Tailwind + shadcn/ui + Amplify auth), the backend API Lambda functions the dashboard consumes, and property-based tests for pure logic.

Language: TypeScript (React 18, Vite). Backend: Python 3.12 Lambda (same SAM stack). Test stack: Vitest + React Testing Library, fast-check for PBT, Playwright for E2E.

Approach:

- **Backend first.** API endpoints are built before the frontend views that consume them, so the frontend can be developed against real responses.
- **Incremental delivery.** Each phase produces a working slice — auth → data → views → flags → tests → deploy.
- **Property tests for pure logic.** The 7 correctness properties from the design target `resolveRole`, heartbeat computation, timestamp formatting, and sort ordering.

## Tasks

- [x] 1. Project scaffold (frontend)
  - [x] 1.1 Initialize Vite + React 18 + TypeScript project in `dashboard/`
    - Run `npm create vite@latest dashboard -- --template react-ts`
    - Configure `tsconfig.json` with strict mode and path aliases (`@/` → `src/`)
    - _Requirements: 1.1_

  - [x] 1.2 Install and configure Tailwind CSS + shadcn/ui
    - Install Tailwind CSS, PostCSS, Autoprefixer
    - Add CSS variables for SiteSpy design tokens (colors, glass tokens, canvas wash) per `design_system.md`
    - Initialize shadcn/ui with the SiteSpy theme
    - Install `@fontsource-variable/inter` and configure as default typeface
    - Install `lucide-react` for icons
    - _Requirements: 12.1_

  - [x] 1.3 Install core dependencies
    - `aws-amplify` for Cognito auth
    - `axios` for API calls
    - `uuid` for `X-Correlation-Id` generation
    - `react-router-dom` for client-side routing
    - _Requirements: 1.1, 1.4_

  - [x] 1.4 Install dev dependencies
    - `vitest`, `@testing-library/react`, `@testing-library/jest-dom`, `jsdom`
    - `fast-check` for property-based tests
    - `@axe-core/react` for accessibility checks
    - `playwright` and `@playwright/test` for E2E
    - Configure `vitest.config.ts` with jsdom environment
    - _Requirements: 12.1_

  - [x] 1.5 Create project structure skeleton
    - Create directory structure: `src/api/`, `src/auth/`, `src/components/`, `src/hooks/`, `src/lib/`, `src/pages/`, `src/types/`
    - Create `src/types/api.ts`, `src/types/auth.ts`, `src/types/site.ts` with TypeScript interfaces from the design document
    - Create `src/lib/constants.ts` with heartbeat thresholds and flag reason enum
    - _Requirements: 1.1_

  - [x] 1.6 Configure environment variables and Amplify setup
    - Create `.env.example` with `VITE_USER_POOL_ID`, `VITE_CLIENT_ID`, `VITE_API_ENDPOINT`
    - Create `src/main.tsx` with Amplify configuration using env vars
    - _Requirements: 1.1, 1.5_

- [x] 2. Backend API endpoints
  - [x] 2.1 Add Cognito JWT authorizer to API Gateway in `template.yaml`
    - Add `CognitoUserPool` and `CognitoUserPoolClient` parameters (or reference existing)
    - Add `Auth` section to `SiteSpyApi` with Cognito authorizer
    - All dashboard endpoints use this authorizer; ingest endpoint remains unauthenticated (token-based)
    - _Requirements: 1.4_

  - [x] 2.2 Implement `GET /v1/sites/{site_id}` Lambda
    - Create `src/sitespy/handlers/sites.py`
    - Query DynamoDB for site record (`PK=TENANT#<tenant_id>`, `SK=SITE#<site_id>`)
    - Query cameras under the site
    - Validate caller has access (check JWT claims against site's tenant + site_access)
    - Return site metadata + camera list
    - Add SAM resource in `template.yaml`
    - _Requirements: 3.1_

  - [x] 2.3 Implement `GET /v1/snapshots/latest` Lambda
    - Create `src/sitespy/handlers/snapshots.py` (or extend sites handler)
    - Accept `site_id` (required) and optional `camera_id` query params
    - Without `camera_id`: return latest snapshot for every camera in the site
    - With `camera_id`: return latest snapshot for that single camera
    - Generate pre-signed S3 URLs (5-minute TTL)
    - Compute `age_seconds` from snapshot timestamp vs. current time
    - Add SAM resource in `template.yaml`
    - _Requirements: 3.1, 3.4, 3.5, 6.1, 6.2, 6.3, 6.4_

  - [x] 2.4 Implement `GET /v1/snapshots` Lambda (paginated list)
    - Accept `site_id`, `camera_id` (both required), `from`, `to`, `limit`, `cursor`
    - Query DynamoDB `IMG#` records for the camera within the date range
    - Generate pre-signed S3 URLs for each snapshot
    - Return paginated response with `next_cursor` and `total_available`
    - Add SAM resource in `template.yaml`
    - _Requirements: 4.4, 4.5, 4.7_

  - [x] 2.5 Implement `POST /v1/flags` Lambda
    - Create `src/sitespy/handlers/flags.py`
    - Validate `site_id`, `camera_id`, `reason`, `note` (note required when reason=other)
    - Check for existing open/acknowledged flag with same camera + reason (duplicate suppression)
    - Write flag record to DynamoDB with GSI1 for status-based queries
    - Return 201 with flag_id, or 200 with existing flag if duplicate
    - Add SAM resource in `template.yaml`
    - _Requirements: 7.5, 7.6_

  - [x] 2.6 Implement `GET /v1/flags` Lambda
    - Accept `status`, `tenant_id`, `site_id`, `camera_id`, `limit`, `cursor` query params
    - Scope results by caller role (super admin: all, tenant admin: own tenant, user: own sites)
    - Include `latest_snapshot` with pre-signed URL for each flag's camera
    - Return paginated response
    - Add SAM resource in `template.yaml`
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.8, 8.9_

  - [x] 2.7 Implement `PATCH /v1/flags/{flag_id}` Lambda
    - Accept `status` and `admin_notes` in body
    - Validate state transitions (open→acknowledged, open→resolved, open→dismissed, acknowledged→resolved, acknowledged→dismissed)
    - Record acting user and timestamp
    - Return 409 on invalid transition
    - Min role: tenant admin
    - Add SAM resource in `template.yaml`
    - _Requirements: 8.6, 8.7_

- [x] 3. Checkpoint — Backend API
  - Ensure all backend Lambda tests pass and endpoints deploy successfully via `sam build`. Ask the user if questions arise.

- [x] 4. Auth layer (frontend)
  - [x] 4.1 Implement `src/auth/roles.ts` — `resolveRole()` pure function
    - Takes `groups: string[]`, `tenantId: string | null`, `siteAccess: string[]`
    - Returns `'super_admin' | 'tenant_admin' | 'user'` based on group membership priority
    - _Requirements: 1.3, 2.1, 2.2, 2.3_

  - [x] 4.2 Implement `src/auth/AuthProvider.tsx`
    - Wrap app in auth context
    - On mount: check Amplify session, extract claims from ID token
    - Call `resolveRole()` to derive effective role
    - Provide `{ user, role, tenantId, siteAccess, isAuthenticated }` via context
    - _Requirements: 1.1, 1.3, 1.5_

  - [x] 4.3 Implement `src/auth/AuthGuard.tsx` and `src/auth/RoleGuard.tsx`
    - AuthGuard: redirect to `/login` if not authenticated
    - RoleGuard: render AccessDenied if role not in `allowedRoles` prop
    - _Requirements: 1.2, 1.6_

  - [x] 4.4 Implement `src/api/client.ts` — Axios instance with auth interceptor
    - Attach `Authorization: Bearer <id_token>` from Amplify session
    - Generate and attach `X-Correlation-Id` header per request
    - Handle 403 responses (set error state for AccessDenied rendering)
    - _Requirements: 1.4, 1.6_

  - [x] 4.5 Implement `src/pages/LoginPage.tsx`
    - Use Amplify `signIn` for login flow
    - Redirect to `/` on success
    - Display error messages for invalid credentials
    - _Requirements: 1.1, 1.2_

- [x] 5. Core views — Site Hero and Gallery
  - [x] 5.1 Implement `src/components/layout/AppShell.tsx`, `Sidebar.tsx`, `Header.tsx`
    - Glass-panel sidebar (240px, collapses to 64px icon-rail below 1280px)
    - Role-aware navigation links (flag console for admins only)
    - Header with site name, heartbeat indicator, user menu
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [x] 5.2 Implement `src/api/sites.ts` and `src/api/snapshots.ts`
    - `getSite(siteId)` → calls `GET /v1/sites/{site_id}`
    - `getLatestSnapshots(siteId)` → calls `GET /v1/snapshots/latest?site_id=X`
    - `getSnapshots(siteId, cameraId, from, to, cursor)` → calls `GET /v1/snapshots`
    - _Requirements: 3.1, 4.4_

  - [x] 5.3 Implement `src/components/site/SiteHero.tsx` and `CameraTile.tsx`
    - Single-camera: full-width image
    - Multi-camera: responsive tile grid, one tile per camera with name label
    - Each tile shows heartbeat dot and flag badge (if applicable)
    - "Camera may be offline" message when `age_seconds > 7200`
    - Click tile → navigate to single-camera focused view
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

  - [x] 5.4 Implement `src/components/shared/ImageWithFallback.tsx`
    - Loading state (skeleton), error state (placeholder + retry), success state
    - On error: show placeholder with "Image unavailable" and Retry button
    - Retry re-fetches snapshot endpoint for fresh pre-signed URL
    - _Requirements: 9.1, 9.2, 9.3_

  - [x] 5.5 Implement `src/components/gallery/Gallery.tsx` and supporting components
    - `Gallery.tsx`: paginated thumbnail grid (newest first)
    - `CameraSelector.tsx`: dropdown, hidden on single-camera sites
    - `DatePicker.tsx`: day filter for `from`/`to` params
    - Handle `next_cursor` for pagination (Load More button)
    - Show placeholder for gaps, neutral empty state for no snapshots
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9_

  - [x] 5.6 Implement `src/components/lightbox/Lightbox.tsx`
    - Full-resolution image overlay
    - Display metadata: timestamp (site timezone), site name, camera name
    - Arrow key and swipe navigation between images of same camera
    - Close action returns to gallery
    - Preload adjacent images
    - _Requirements: 5.1, 5.2, 5.3, 5.4_

  - [x] 5.7 Implement `src/components/shared/Timestamp.tsx`
    - Accepts `utc` (ISO string) and `timezone` (IANA string)
    - Renders formatted time using `Intl.DateTimeFormat` with site timezone
    - All timestamp display in the app uses this component
    - _Requirements: 10.1, 10.2_

  - [x] 5.8 Implement `src/lib/timezone.ts` — `formatTimestamp()` pure function
    - Takes UTC ISO string, IANA timezone, and format options
    - Returns formatted string in the target timezone
    - Used by the `<Timestamp />` component
    - _Requirements: 10.1, 10.2_

- [x] 6. Heartbeat and status indicators
  - [x] 6.1 Implement `src/lib/heartbeat.ts` — pure functions
    - `computeCameraStatus(ageSeconds)`: returns `'healthy' | 'warning' | 'critical'`
    - `computeAggregateStatus(ageValues[])`: returns status of worst camera
    - `isCameraOffline(ageSeconds)`: returns true when > 7200
    - Thresholds: warning=5400, critical=10800, offline=7200
    - _Requirements: 6.2, 6.3, 6.4_

  - [x] 6.2 Implement `src/components/heartbeat/HeartbeatDot.tsx` and `HeartbeatSummary.tsx`
    - `HeartbeatDot`: single camera status (icon + color + text label)
    - `HeartbeatSummary`: persistent header indicator, worst camera status
    - Hover reveals per-camera breakdown
    - Status conveyed by color AND icon/text (not color alone)
    - _Requirements: 6.1, 6.5, 6.6_

- [x] 7. Flag a Camera flow
  - [x] 7.1 Implement `src/api/flags.ts`
    - `createFlag(siteId, cameraId, reason, note)` → `POST /v1/flags`
    - `getFlags(params)` → `GET /v1/flags`
    - `updateFlag(flagId, status, adminNotes)` → `PATCH /v1/flags/{flag_id}`
    - _Requirements: 7.5, 8.6_

  - [x] 7.2 Implement `src/components/flags/FlagButton.tsx` and `FlagForm.tsx`
    - FlagButton: present on camera tiles, focused view, and lightbox
    - FlagForm: modal with Reason dropdown + Note textarea
    - Note required when Reason = "Other", optional otherwise, max 1000 chars
    - Pre-fills camera name
    - On submit: show confirmation toast or duplicate toast
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6_

  - [x] 7.3 Implement `src/components/flags/FlagBadge.tsx`
    - Warning badge on camera tiles when open/acknowledged flag exists
    - Shows flag reason (e.g., "⚠ Flagged: Physical damage")
    - Click shows flag details and status
    - _Requirements: 7.7, 7.8_

- [x] 8. Flag Review Console
  - [x] 8.1 Implement `src/pages/FlagConsolePage.tsx` and `src/components/flags/FlagConsole.tsx`
    - Route: `/flags`, accessible to Tenant Admin + Super Admin only (RoleGuard)
    - Table with flag rows: site name, camera name, reason, note, source, raised-by, raised-at, status badge, latest snapshot thumbnail
    - Tenant name column visible for Super Admin only
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_

  - [x] 8.2 Implement `src/components/flags/FlagRow.tsx` and `FlagActionModal.tsx`
    - Action buttons: Acknowledge, Resolve, Dismiss
    - Modal for optional admin notes before calling `PATCH /flags/{flag_id}`
    - _Requirements: 8.6, 8.7_

  - [x] 8.3 Implement flag console filters and sort
    - Filters: status (default: open + acknowledged), reason, site, camera, source
    - Sort: newest first (default), oldest first, by reason
    - Deep link: click flag row → navigate to gallery with camera pre-selected
    - _Requirements: 8.8, 8.9, 8.10_

- [x] 9. Error and empty states
  - [x] 9.1 Implement error handling components
    - `src/components/shared/ErrorToast.tsx`: network error toast with Retry button
    - `src/components/shared/AccessDenied.tsx`: full-page 403 state with context and link back
    - `src/components/shared/EmptyState.tsx`: reusable empty state card
    - Global error boundary wrapping the app (never a white screen)
    - _Requirements: 11.1, 11.2, 11.3, 11.4_

  - [x] 9.2 Wire error states into views
    - No cameras: helpful card with role-appropriate CTA
    - No snapshots in range: neutral empty state with date picker highlighted
    - Stale camera: in-view banner with last timestamp + "Raise flag" button
    - Network errors: toast with retry, exponential backoff (1s, 2s, 4s, then manual)
    - _Requirements: 11.1, 11.2, 11.3, 11.4_

- [x] 10. Checkpoint — Frontend integration
  - Ensure all components render correctly, routing works, and API calls succeed against the deployed backend. Ask the user if questions arise.

- [x] 11. Property-based tests (fast-check)
  - [x] 11.1 Write property test: Role Resolution Determinism (Property 1)
    - File: `src/auth/__tests__/roles.property.test.ts`
    - For any valid combination of groups, tenantId, siteAccess: `resolveRole()` returns exactly one role following priority order
    - Min 100 iterations
    - **Property 1: Role Resolution Determinism**
    - **Validates: Requirements 1.3**

  - [x] 11.2 Write property test: Camera Heartbeat Status Computation (Property 4)
    - File: `src/lib/__tests__/heartbeat.property.test.ts`
    - For any non-negative `age_seconds`: `computeCameraStatus()` returns correct status per thresholds
    - Verify monotonicity: increasing age never improves status
    - Min 100 iterations
    - **Property 4: Camera Heartbeat Status Computation**
    - **Validates: Requirements 3.4, 6.2, 6.3, 6.4**

  - [x] 11.3 Write property test: Aggregate Heartbeat Equals Worst Camera (Property 5)
    - File: `src/lib/__tests__/heartbeat.property.test.ts`
    - For any non-empty array of age values: `computeAggregateStatus()` equals `computeCameraStatus(max(ages))`
    - Min 100 iterations
    - **Property 5: Aggregate Heartbeat Equals Worst Camera**
    - **Validates: Requirements 6.1**

  - [x] 11.4 Write property test: Snapshot Chronological Ordering (Property 6)
    - File: `src/lib/__tests__/snapshots.property.test.ts`
    - For any list of snapshots with distinct timestamps: gallery sort produces strictly descending timestamps
    - Min 100 iterations
    - **Property 6: Snapshot Chronological Ordering**
    - **Validates: Requirements 4.5**

  - [x] 11.5 Write property test: Timestamp Timezone Formatting (Property 7)
    - File: `src/lib/__tests__/timezone.property.test.ts`
    - For any valid UTC timestamp and IANA timezone: `formatTimestamp()` round-trips within 1-second precision
    - Min 100 iterations
    - **Property 7: Timestamp Timezone Formatting**
    - **Validates: Requirements 5.2, 10.1**

  - [x] 11.6 Write property test: Site Access Filtering (Property 3)
    - File: `src/auth/__tests__/site-access.property.test.ts`
    - For any user with N site IDs in siteAccess: rendered site list contains exactly those N sites
    - Min 100 iterations
    - **Property 3: Site Access Filtering**
    - **Validates: Requirements 2.3**

- [ ] 12. E2E tests (Playwright)
  - [x] 12.1 Write E2E test: Login → Site View flow
    - Login with valid credentials → verify hero view renders with camera images
    - Verify redirect to login when unauthenticated
    - _Requirements: 1.1, 1.2, 3.1_

  - [x] 12.2 Write E2E test: Gallery Browse flow
    - Navigate to gallery → filter by date → paginate → open lightbox → verify metadata
    - _Requirements: 4.4, 4.5, 4.6, 5.1, 5.2_

  - [x] 12.3 Write E2E test: Raise Flag flow
    - Click flag button → fill form → submit → verify confirmation toast
    - Verify duplicate suppression toast on second submit
    - _Requirements: 7.1, 7.2, 7.5, 7.6_

  - [x] 12.4 Write E2E test: Admin Flag Console flow
    - Login as tenant admin → view flags → acknowledge a flag → verify status change
    - _Requirements: 8.1, 8.6, 8.7_

  - [x] 12.5 Write E2E test: Keyboard navigation and accessibility
    - Tab through all interactive elements → verify visible focus rings
    - Verify minimum 40×40px hit targets on interactive elements
    - Run axe-core accessibility audit on key pages
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6_

- [x] 13. Deployment — Amplify Hosting setup
  - [x] 13.1 Create `amplify.yml` build spec
    - Configure build command: `npm run build`
    - Configure output directory: `dist`
    - Set environment variables: `VITE_USER_POOL_ID`, `VITE_CLIENT_ID`, `VITE_API_ENDPOINT`
    - Configure SPA fallback (rewrites all paths to `index.html`)
    - _Requirements: 1.1_

  - [x] 13.2 Configure Amplify Hosting in `eu-west-2`
    - Connect Git repository branch to Amplify Hosting
    - Set up custom domain (if applicable)
    - Verify deployment builds and serves the SPA correctly
    - See `dashboard/DEPLOY.md` for step-by-step guide
    - _Requirements: 1.1_

- [ ] 14. Final checkpoint
  - Ensure all tests pass (Vitest unit + property tests, Playwright E2E). Verify the deployed dashboard loads, authenticates, and displays site data. Ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP delivery.
- Backend endpoints (task 2) must be deployed before frontend integration testing.
- Property tests target pure logic only — UI rendering is covered by E2E and unit tests.
- The timelapse generator and admin management UIs are out of scope for this spec.
- All timestamps use UTC internally; display uses the site's timezone via the shared `<Timestamp />` component.
