# SiteSpy — Authentication & Multi-Tenant Auth Handover

This document explains how to set up and interact with the SiteSpy authentication system. The backend uses AWS Cognito for identity, with role-based access control enforced at the API layer.

---

## 1. Cognito Configuration

### User Pool Details

| Setting | Value |
|---|---|
| Region | `eu-west-2` (London) |
| User Pool ID | Provided as `VITE_USER_POOL_ID` |
| App Client ID | Provided as `VITE_CLIENT_ID` |
| App Client Secret | None (public web client) |

### Environment Variables

```env
VITE_USER_POOL_ID=eu-west-2_XXXXXXXXX
VITE_CLIENT_ID=xxxxxxxxxxxxxxxxxxxxxxxxxx
VITE_API_ENDPOINT=https://xxxxxxxxxx.execute-api.eu-west-2.amazonaws.com/prod
```

---

## 2. Amplify Setup

Use `aws-amplify` v6+ for the auth integration.

### Configuration

```typescript
import { Amplify } from 'aws-amplify';

Amplify.configure({
  Auth: {
    Cognito: {
      userPoolId: import.meta.env.VITE_USER_POOL_ID,
      userPoolClientId: import.meta.env.VITE_CLIENT_ID,
    },
  },
});
```

### Sign In

```typescript
import { signIn } from 'aws-amplify/auth';

const result = await signIn({
  username: email,
  password: password,
});

if (result.isSignedIn) {
  // Redirect to dashboard
} else if (result.nextStep.signInStep === 'CONFIRM_SIGN_IN_WITH_NEW_PASSWORD_REQUIRED') {
  // First login — user must set a new password
}
```

### First Login (New Password Required)

Users are created by admins and receive an invitation email with a temporary password. On first login, Cognito forces a password change:

```typescript
import { confirmSignIn } from 'aws-amplify/auth';

await confirmSignIn({
  challengeResponse: newPassword,
});
```

### Sign Out

```typescript
import { signOut } from 'aws-amplify/auth';

await signOut(); // Clears all tokens
await signOut({ global: true }); // Signs out of all devices
```

### Get Current Session & Token

```typescript
import { fetchAuthSession, getCurrentUser } from 'aws-amplify/auth';

// Get the ID token for API calls
const session = await fetchAuthSession();
const idToken = session.tokens?.idToken?.toString();

// Get user info
const user = await getCurrentUser();
```

### Token Refresh

Amplify handles token refresh automatically. The ID token expires after 1 hour, but `fetchAuthSession()` will transparently refresh it using the refresh token (valid 30 days). You don't need to handle refresh manually.

---

## 3. Role System

Roles are determined by **Cognito group membership**, extracted from the ID token's `cognito:groups` claim.

### Three Roles

| Role | Cognito Group | What they see |
|---|---|---|
| **Super Admin** | `SuperAdmins` | Everything. All tenants, all sites, all flags. |
| **Tenant Admin** | `TenantAdmins` | All sites within their tenant. Can manage users and resolve flags. |
| **User** | (no group) | Only the specific sites listed in their `custom:site_access` attribute. |

### Extracting Role from Token

```typescript
import { fetchAuthSession } from 'aws-amplify/auth';

interface UserRole {
  role: 'super_admin' | 'tenant_admin' | 'user';
  tenantId: string | null;
  siteAccess: string[];
}

async function resolveUserRole(): Promise<UserRole> {
  const session = await fetchAuthSession();
  const idToken = session.tokens?.idToken;

  if (!idToken) throw new Error('Not authenticated');

  const payload = idToken.payload;

  // Groups come as an array of strings
  const groups: string[] = (payload['cognito:groups'] as string[]) || [];

  let role: 'super_admin' | 'tenant_admin' | 'user';
  if (groups.includes('SuperAdmins')) {
    role = 'super_admin';
  } else if (groups.includes('TenantAdmins')) {
    role = 'tenant_admin';
  } else {
    role = 'user';
  }

  // Custom attributes
  const tenantId = (payload['custom:tenant_id'] as string) || null;
  const rawSiteAccess = (payload['custom:site_access'] as string) || '';
  const siteAccess = rawSiteAccess
    .split(',')
    .map(s => s.trim())
    .filter(Boolean);

  return { role, tenantId, siteAccess };
}
```

### Token Claims by Role

**Regular User:**
```json
{
  "sub": "a1b2c3d4-...",
  "email": "jane@acme.example.com",
  "cognito:groups": [],
  "custom:tenant_id": "acme_corp",
  "custom:site_access": "site_001,site_002"
}
```

**Tenant Admin:**
```json
{
  "sub": "e5f6g7h8-...",
  "email": "ops@acme.example.com",
  "cognito:groups": ["TenantAdmins"],
  "custom:tenant_id": "acme_corp"
}
```

**Super Admin:**
```json
{
  "sub": "i9j0k1l2-...",
  "email": "admin@sitespy.io",
  "cognito:groups": ["SuperAdmins"]
}
```

Note: Super admins have no `custom:tenant_id` or `custom:site_access`.

---

## 4. Role-Based UI Rendering

### Navigation Visibility

| UI Element | Super Admin | Tenant Admin | User |
|---|---|---|---|
| Tenant picker | ✅ | ❌ | ❌ |
| Site picker (all sites in tenant) | ✅ | ✅ | ❌ |
| Site picker (assigned sites only) | — | — | ✅ |
| Camera picker | ✅ | ✅ | ✅ |
| Flag review console (`/flags`) | ✅ | ✅ | ❌ |
| Raise flag button | ✅ | ✅ | ✅ |
| User management (`/admin/users`) | ✅ | ✅ | ❌ |
| Tenant management (`/admin/tenants`) | ✅ | ❌ | ❌ |
| Site/camera management (`/admin/sites`) | ✅ | ✅ | ❌ |

### Route Protection

```typescript
// Example route guard
function RequireRole({ minRole, children }: { minRole: string; children: React.ReactNode }) {
  const { role } = useAuth();

  const hierarchy = { user: 0, tenant_admin: 1, super_admin: 2 };

  if (hierarchy[role] < hierarchy[minRole]) {
    return <AccessDeniedPage />;
  }

  return children;
}
```

### Site Access Enforcement

For regular users, the `custom:site_access` claim is a comma-separated list of site IDs they can view. The frontend should:

1. Only show sites from this list in the site picker
2. Redirect to "Access Denied" if a user navigates to a site not in their list
3. The API will return `403` anyway, but catching it client-side gives a better UX

For tenant admins, all sites in their tenant are accessible — no `site_access` filtering needed.

---

## 5. API Authorization Rules

### Super Admin Specifics

Super admins have no implicit tenant. When calling any endpoint that requires a tenant context, they MUST pass `?tenant_id=<id>` as a query parameter:

```typescript
// Super admin fetching a site
api.get('/v1/sites/site_001', { params: { tenant_id: 'acme_corp' } });

// Super admin listing flags
api.get('/v1/flags', { params: { tenant_id: 'acme_corp' } });

// Super admin raising a flag
api.post('/v1/flags', body, { params: { tenant_id: 'acme_corp' } });
```

Tenant admins and users don't need this — their tenant is inferred from the token.

### 403 Handling

When the API returns `403 ACCESS_DENIED`:

```json
{
  "error": "ACCESS_DENIED",
  "message": "You do not have access to this site."
}
```

Display a full-page "Access Denied" state. Never silently swallow 403s — the user needs to know they're trying to access something outside their scope.

---

## 6. Setting Up Cognito (From Scratch)

If you need to create the Cognito User Pool for a new environment:

### Step 1: Create the User Pool

```bash
aws cognito-idp create-user-pool \
  --pool-name sitespy-prod \
  --region eu-west-2 \
  --auto-verified-attributes email \
  --username-attributes email \
  --schema \
    Name=email,Required=true,Mutable=true \
    Name=custom:tenant_id,AttributeDataType=String,Mutable=true \
    Name=custom:site_access,AttributeDataType=String,Mutable=true \
  --policies 'PasswordPolicy={MinimumLength=12,RequireUppercase=true,RequireLowercase=true,RequireNumbers=true,RequireSymbols=false}' \
  --admin-create-user-config 'AllowAdminCreateUserOnly=true'
```

Note: `AllowAdminCreateUserOnly=true` means users cannot self-register. All users are created by admins.

### Step 2: Create Groups

```bash
USER_POOL_ID="eu-west-2_XXXXXXXXX"

aws cognito-idp create-group \
  --user-pool-id $USER_POOL_ID \
  --group-name SuperAdmins \
  --region eu-west-2

aws cognito-idp create-group \
  --user-pool-id $USER_POOL_ID \
  --group-name TenantAdmins \
  --region eu-west-2
```

### Step 3: Create App Client (No Secret)

```bash
aws cognito-idp create-user-pool-client \
  --user-pool-id $USER_POOL_ID \
  --client-name sitespy-dashboard \
  --no-generate-secret \
  --explicit-auth-flows ALLOW_USER_SRP_AUTH ALLOW_REFRESH_TOKEN_AUTH \
  --region eu-west-2
```

### Step 4: Create the First Super Admin

```bash
aws cognito-idp admin-create-user \
  --user-pool-id $USER_POOL_ID \
  --username admin@sitespy.io \
  --temporary-password "TempPass123!" \
  --user-attributes Name=email,Value=admin@sitespy.io \
  --region eu-west-2

aws cognito-idp admin-add-user-to-group \
  --user-pool-id $USER_POOL_ID \
  --username admin@sitespy.io \
  --group-name SuperAdmins \
  --region eu-west-2
```

### Step 5: Create a Tenant Admin

```bash
aws cognito-idp admin-create-user \
  --user-pool-id $USER_POOL_ID \
  --username ops@acme.example.com \
  --temporary-password "TempPass123!" \
  --user-attributes \
    Name=email,Value=ops@acme.example.com \
    Name=custom:tenant_id,Value=acme_corp \
  --region eu-west-2

aws cognito-idp admin-add-user-to-group \
  --user-pool-id $USER_POOL_ID \
  --username ops@acme.example.com \
  --group-name TenantAdmins \
  --region eu-west-2
```

### Step 6: Create a Regular User

```bash
aws cognito-idp admin-create-user \
  --user-pool-id $USER_POOL_ID \
  --username jane@acme.example.com \
  --temporary-password "TempPass123!" \
  --user-attributes \
    Name=email,Value=jane@acme.example.com \
    Name=custom:tenant_id,Value=acme_corp \
    Name=custom:site_access,Value="site_001,site_002" \
  --region eu-west-2
```

No group membership needed for regular users — absence of a group means "user" role.

---

## 7. Token Storage & Security

**Rules:**
- Never store the raw JWT in `localStorage` — use Amplify's built-in secure token management
- Amplify stores tokens in `localStorage` by default but handles refresh transparently
- The ID token is what you send to the API (not the access token)
- Never log or expose token values in the UI

**Token lifecycle:**
- ID Token: expires after 1 hour
- Access Token: expires after 1 hour
- Refresh Token: expires after 30 days
- Amplify auto-refreshes using the refresh token when you call `fetchAuthSession()`

---

## 8. Auth State Management Pattern

```typescript
import { createContext, useContext, useEffect, useState } from 'react';
import { fetchAuthSession, getCurrentUser, signOut } from 'aws-amplify/auth';
import { Hub } from 'aws-amplify/utils';

interface AuthState {
  isAuthenticated: boolean;
  isLoading: boolean;
  role: 'super_admin' | 'tenant_admin' | 'user' | null;
  tenantId: string | null;
  siteAccess: string[];
  email: string | null;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState>(/* ... */);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AuthState>({
    isAuthenticated: false,
    isLoading: true,
    role: null,
    tenantId: null,
    siteAccess: [],
    email: null,
    logout: async () => { await signOut(); },
  });

  useEffect(() => {
    checkAuth();

    // Listen for auth events (sign in, sign out, token refresh)
    const unsubscribe = Hub.listen('auth', ({ payload }) => {
      switch (payload.event) {
        case 'signedIn':
          checkAuth();
          break;
        case 'signedOut':
          setState(prev => ({ ...prev, isAuthenticated: false, role: null }));
          break;
      }
    });

    return unsubscribe;
  }, []);

  async function checkAuth() {
    try {
      const session = await fetchAuthSession();
      const idToken = session.tokens?.idToken;

      if (!idToken) {
        setState(prev => ({ ...prev, isAuthenticated: false, isLoading: false }));
        return;
      }

      const payload = idToken.payload;
      const groups: string[] = (payload['cognito:groups'] as string[]) || [];

      let role: 'super_admin' | 'tenant_admin' | 'user';
      if (groups.includes('SuperAdmins')) role = 'super_admin';
      else if (groups.includes('TenantAdmins')) role = 'tenant_admin';
      else role = 'user';

      setState({
        isAuthenticated: true,
        isLoading: false,
        role,
        tenantId: (payload['custom:tenant_id'] as string) || null,
        siteAccess: ((payload['custom:site_access'] as string) || '')
          .split(',').map(s => s.trim()).filter(Boolean),
        email: (payload['email'] as string) || null,
        logout: async () => { await signOut(); },
      });
    } catch {
      setState(prev => ({ ...prev, isAuthenticated: false, isLoading: false }));
    }
  }

  return <AuthContext.Provider value={state}>{children}</AuthContext.Provider>;
}

export const useAuth = () => useContext(AuthContext);
```

---

## 9. Login Flow Summary

```
1. User opens app
2. AuthProvider checks for existing session (fetchAuthSession)
3. No session → redirect to /login
4. User enters email + password
5. signIn() called
   ├── Success → extract role from token → redirect to dashboard
   └── NEW_PASSWORD_REQUIRED → show "set new password" form → confirmSignIn()
6. On every API call: fetchAuthSession() → attach idToken as Bearer header
7. On 401 from API: redirect to /login (token expired and refresh failed)
```

---

## 10. Testing Auth Locally

For local development, you'll be pointed at the real Cognito User Pool in `eu-west-2`. There's no local Cognito emulator that's worth using.

Test credentials will be provided separately. The seed script (`scripts/seed-dev.sh`) creates a test user with:
- Email: `fin@test.com`
- Role: Tenant Admin
- Tenant: `acme`
- Site access: `site_01`

After running the seed script, sign in with this user to test the full flow.
