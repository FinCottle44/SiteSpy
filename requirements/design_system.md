# SiteSpy — Design System

## 1. Product Identity

- **Product name:** SiteSpy
- **Tagline (working):** Always watching. Never in the way.
- **Voice:** confident, technical-but-friendly, construction-industry competent.

## 2. Visual Direction

**Liquid Glass.** Surfaces are translucent, softly blurred panels layered over a subtle ambient background. Elevation is expressed through blur intensity, border luminosity, and a quiet shadow — not through heavy solid cards. The aesthetic borrows from iOS 26's liquid glass language while keeping the content hierarchy appropriate for a data-dense professional dashboard.

Guiding principles:
- Glass surfaces need something to refract. The app canvas is never a flat single color — it carries a low-contrast gradient or abstract wash so glass panels read as glass.
- Translucency is tasteful, not gratuitous. Dense data tables and form inputs sit on opaque surfaces; only containers, cards, and navigation use glass.
- Motion is calm. Panels ease in and out, they don't spring.
- Accessibility first. Text contrast on glass meets WCAG AA even at the lowest practical surface opacity.

## 3. Typography

- **Typeface:** Inter (variable) for all UI. Loaded via `@fontsource-variable/inter` to avoid Google Fonts CDN and keep everything self-hosted.
- **Scale** (rem, matches Tailwind defaults with one tweak):
  - `text-xs` 0.75rem — captions, metadata.
  - `text-sm` 0.875rem — body, table cells.
  - `text-base` 1rem — default paragraph.
  - `text-lg` 1.125rem — card titles.
  - `text-xl` 1.25rem — section headers.
  - `text-2xl` 1.5rem — page titles.
  - `text-4xl` 2.25rem — hero numbers.
- **Weights:** 400 (body), 500 (emphasis), 600 (headings), 700 (rarely, for hero figures).
- **Tabular numerals** (`font-variant-numeric: tabular-nums`) for all timestamps, ages, counts, and table numbers.

## 4. Color Tokens

From the approved palette. Expressed as CSS variables so dark mode can be retrofitted later without component rewrites.

| Token | Hex | Usage |
| :--- | :--- | :--- |
| `--color-canvas` | `#F5F7FB` | Page background |
| `--color-surface` | `#FFFFFF` | Opaque cards, form surfaces, modals |
| `--color-ink` | `#33394C` | Default text, icons |
| `--color-ink-muted` | `#33394C` @ 60% | Secondary text, placeholders |
| `--color-primary` | `#4E7CFF` | Primary actions, links, active nav, heartbeat healthy |
| `--color-accent` | `#7033FF` | Secondary emphasis, chart series, admin badges |
| `--color-danger` | `#F65164` | Flags, destructive actions, heartbeat offline |
| `--color-warning` | `#FFB547` | Heartbeat warning, caution states (derived, not in source palette) |
| `--color-success` | `#2FBF71` | Resolved flags, healthy states (derived) |
| `--color-hairline` | `rgba(51, 57, 76, 0.08)` | 1px separators |

### Glass tokens

| Token | Value | Purpose |
| :--- | :--- | :--- |
| `--glass-panel` | `rgba(255, 255, 255, 0.70)` + `backdrop-filter: blur(24px) saturate(140%)` | Default card / navigation surface |
| `--glass-raised` | `rgba(255, 255, 255, 0.82)` + `blur(28px)` + stronger shadow | Modals, popovers |
| `--glass-sunken` | `rgba(51, 57, 76, 0.04)` + `blur(12px)` | Inline wells, search inputs on glass |
| `--glass-border` | `1px solid rgba(255, 255, 255, 0.55)` | Top/left edge highlight |
| `--glass-shadow` | `0 8px 32px rgba(51, 57, 76, 0.08), 0 1px 2px rgba(51, 57, 76, 0.04)` | Panel elevation |

### Canvas wash

The app canvas carries a gentle radial gradient so glass panels have something to refract:

```css
background:
  radial-gradient(1200px 600px at 10% -10%, rgba(78, 124, 255, 0.10), transparent 60%),
  radial-gradient(1000px 500px at 110% 10%, rgba(112, 51, 255, 0.08), transparent 60%),
  var(--color-canvas);
```

Site view hero images and timelapse renders introduce real content behind glass navigation — the wash is only for empty-canvas contexts (login, empty states, admin tables).

## 5. Components

Built on **shadcn/ui** primitives (Radix under the hood) for accessibility and consistency, restyled to the SiteSpy token set. Key component treatments:

- **Buttons:** 40px height default. Primary is solid `--color-primary` with a subtle inner gloss (`linear-gradient(180deg, rgba(255,255,255,0.16), transparent)`). Ghost buttons sit on glass-sunken surfaces. All buttons have `min-width: 80px` to avoid tiny tap targets.
- **Cards:** glass-panel by default. Rounded 16px. Internal padding 24px. Card titles use `text-lg / 600`.
- **Inputs:** opaque `--color-surface`, 1px `--color-hairline` border, 12px radius, 40px height. Focus state: 2px ring in `--color-primary` @ 40%.
- **Nav sidebar:** full-height glass-panel, 240px wide, collapses to 64px icon-rail on `<1280px`. Active item gets a pill-shaped `--color-primary` background at 12% alpha with a 3px leading bar.
- **Data tables:** opaque surface inside a glass card. Row hover tints 4% ink. Sortable columns use a soft chevron, not a shouty arrow.
- **Status badges:** pill, 20px tall, uppercase text-xs, tinted background at 15% alpha over the relevant state color.
- **Modals:** glass-raised panel, centered, max-width 560px for forms / 960px for content. Backdrop uses `rgba(51, 57, 76, 0.32)` + `backdrop-filter: blur(6px)`.
- **Toasts:** glass-raised pill in the top-right, 4s default dismiss, explicit close button.
- **Empty states:** large neutral illustration or muted SVG glyph, one-sentence explanation, one primary CTA.

## 6. Layout

- **Viewport baseline:** designed first for 1440×900. Responsive breakpoints: `sm` 640, `md` 768, `lg` 1024, `xl` 1280, `2xl` 1536.
- **Grid:** 12-column, 24px gutter on desktop, 16px on tablet, 12px on mobile.
- **Max content width:** 1440px, centered.
- **Spacing scale:** 4/8/12/16/24/32/48/64. No off-grid spacing.

## 7. Iconography

Lucide React. 20px default. 1.5px stroke. Icons inherit `currentColor` and must never carry their own color.

## 8. Motion

- Panel enter: 180ms `cubic-bezier(0.2, 0.8, 0.2, 1)`, opacity + 8px translate.
- Hover lift: 80ms ease-out, shadow intensity only (no scale).
- Focus ring: instant.
- Prefers-reduced-motion: all non-essential motion disabled.

## 9. Accessibility

- Every glass surface must achieve contrast ratio ≥ 4.5:1 for body text against its effective backdrop. Verify by rendering the surface over the app canvas wash, not over pure white.
- Focus ring is 2px `--color-primary` @ 80% alpha on every interactive element, never removed.
- Interactive targets minimum 40×40px.
- Skip-to-content link on every page.
- Status is never conveyed by color alone — always paired with an icon or label.

## 10. Dark Mode

Out of scope for Phase 0. Tokens are defined as CSS variables specifically so a dark theme can be swapped in later without touching component code.
