# Dashboard front-end design research

Researched 2026-11-18 for the admin dashboard redesign (`src/promise_monitor/dashboard/`).
Fetches used `curl -A promise-monitor-research/1.0`, one request at a time, about 1 per second. Nothing was blocked, so WebFetch was not needed.
Raw downloads were kept outside the repo. The file and line references below point at those public URLs.

Constraints this research assumes (from `src/promise_monitor/dashboard/app.py:97-100`):
`default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'`.
What follows from that header:
- There is no `font-src`, so a self-hosted web font is blocked until `font-src 'self'` is added ([MDN font-src](https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Content-Security-Policy/font-src)).
- `style-src 'self'` without `'unsafe-inline'` blocks `<style>` blocks and `style="…"` attributes ([MDN style-src](https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Content-Security-Policy/style-src)).
- `img-src 'self'` blocks `data:` images, so a logo must be a same-origin file or inline `<svg>` markup.

---

## Part 1: AdvoKC brand (primary sources)

### 1.1 Domains

- `https://advokc.ng` and `https://advokc.org` both return 200 and serve the same Webflow site. Both have site id `615ce181227811348a82a252`, and the `<html>` tags differ only in `data-wf-domain="advokc.ng"` vs `"advokc.org"`.
- The site's contact address is `mailto:admin@advokc.ng`, so **advokc.ng** is treated here as the primary domain.
- The site links to `https://www.promisetracker.ng`.
- Source for all of the above: the advokc.ng and advokc.org homepage HTML.
- AdvoKC stylesheet: `https://cdn.prod.website-files.com/615ce181227811348a82a252/css/advokc.webflow.shared.2355d7783.css` (the "AdvoKC CSS" below).
- promisetracker.ng is a Vite/React SPA. Its HTML links `/assets/index-CnIuzEWo.css` and `/assets/index-CxWRBiNo.js` (the "PT CSS" and "PT JS" below). Source: `https://promisetracker.ng/` HTML.

### 1.2 Colours

**AdvoKC named brand tokens** (AdvoKC CSS `:root`):

| Token (as named by AdvoKC) | Hex | Where it's used (AdvoKC CSS) |
|---|---|---|
| `--advokc-primary` | **#77121A** | `.advokc-button` background, links, coloured headings. Used 41 times via `var()`, and the literal `#77121a` appears 151 times, the most common colour after `#fff`. |
| `--advokc-button-hover` | **#842930** | `:hover`/`:focus` of `.uui-button*`, `.advokc-primary-button:hover`, current breadcrumb, `.uui-badge.is-primary` |
| `--advokc-pink-colour` | **#FADCDC** | Hover/focus backgrounds of nav links, inputs and dropdowns; success message background |
| `--advokc-black-text-colour` | **#231F20** | Body text (108 uses) |
| `--maroon` | #642728 | Used once |
| `--dark-red` | #A10C0C | Used 5 times |
| `--untitled-ui--civic-fest` | #D98305 | Civic Fest campaign amber |

The AdvoKC CSS also carries leftover Webflow template palettes (Untitled UI purple `#7F56D9`, Brix, and others). These are template residue, not brand colours. The primary CTA is `.advokc-button { background: var(--advokc-primary); color:#fff; border-radius:8px; font-family: Mafletype… }`.

**Promise Tracker** (PT CSS, counted by frequency):

| Hex | Uses | Where it's used |
|---|---|---|
| **#77121A** | 78 | Same maroon. Sub-menu highlight, `.btn-div button`, active toggles, `.state-dot.active`. Also `--ftm-maroon` and `--sp-red`. |
| **#5F1016** | 10 | Pressed/active state: `.pagination1 button.active1`, `.tenure-pill.is-active`, `.tenure-select:hover` |
| `#5E0E15` | – | `--ftm-maroon-dark` |
| **#0C1821** | 35 | Near-black navy: `.footer-con`, `#text-div`, dark sections |
| **#1B3A50** | 12 | Navy (`--ftm-navy`) for tile titles and form-sheet text |
| **#5C6165** | 17 | Muted grey text (`--ftm-grey`) |
| #D63321 | 8 | Bright red CTA wrappers (`.sub-btn`, `.pledge-submit-wrapper`) |
| #FCD7DA / #FFF6F6 / #FDEBEB | 8 / 7 / 5 | Pink tints for panels |
| #BB3C2E | – | Favicon front hexagon (see 1.4) |

**Status colours**, defined in the PT JS. The same map appears three times: the doughnut chart, the progress-bar gradients, and a `switch` in the status component.

```
kept:#00BB03  broken:#E20102  intheworks:#0073E6  stalled:#FAE916
compromised:#FFA500  notyetrated:#D9D9D9  (unknown: #9B9999)
chart labels: ["Not Rated","In Works","Stalled","Compromised","Kept","Broken"]
```

These are saturated fill colours with no text on them. Kept, stalled and not-rated fail 3:1 against white (see 1.6), so the dashboard should keep the **hue** and use darker or tinted variants for text badges.

### 1.3 Typography

| Site | Fonts actually used | Licence and whether we can vendor it |
|---|---|---|
| advokc.ng | **MafleType**, the most common family (181 rules, e.g. `.advokc-button`), loaded from `…/69b2c4342276fb0ee60b74e7_MafleType-SemiBold.ttf`. Also Inter (144), Raleway (85), Poppins (50) and Linear Grotesk (25), with Google families loaded via `webfont.js` (AdvoKC HTML `WebFont.load`). | MafleType is a **purchased commercial font**. Its `Readme.txt` on the CDN says "Thank you for purchasing and using Mafle Type… Pradipta Creative x Lettertype Studio" (`…/69b2c43400a6e51615f74dc5_Readme.txt`). We can't redistribute it without checking AdvoKC's licence. |
| promisetracker.ng | **Helvetica Neue** Light and Medium (`hel-ord`, `hel`: 218 rules), **Bodoni 72** Bold and Italic for display headings, and Inter Regular, all self-hosted under `/assets/*.otf` (PT CSS `@font-face`). | Helvetica Neue and Bodoni 72 are commercial (Linotype/Apple). **Don't vendor them.** |
| Common open option | **Inter** is used on both sites. | SIL Open Font License 1.1 ([rsms/inter LICENSE.txt](https://github.com/rsms/inter/blob/master/LICENSE.txt)), so self-hosting is allowed. |

Recommendation: use a **system UI stack** by default, since it needs no CSP change and no download:
`system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif`.
Apple devices will then render Helvetica Neue or SF, which is close to the tracker's look. If a brand font is wanted, self-host **Inter** variable WOFF2 under `/static/fonts/`, include `LICENSE.txt`, and add `font-src 'self'` to the CSP. Use `ui-monospace, SFMono-Regular, Menlo, Consolas, monospace` for IDs and hashes.

### 1.4 Logos and favicons

| Asset | URL | Notes |
|---|---|---|
| **AdvoKC wordmark, SVG** | `https://cdn.prod.website-files.com/615ce181227811348a82a252/69dcd3e47241304ed9a0ef20_Group.svg` | 1091×237 "advokc" wordmark in the footer (AdvoKC HTML `.div-block-122`). It is pure `<path>` elements with a single fill `#77121A`: no scripts, fonts or external references. **Best candidate to vendor**: the fill can be swapped for `currentColor` so it works in dark mode. |
| AdvoKC wordmark, PNG | `https://cdn.prod.website-files.com/615ce181227811348a82a252/6687551fae9242f00658cdb3_Frame%20427318752.png` | 167×37 maroon wordmark in the navbar (`.uui-logo_logotype-4`) |
| AdvoKC "badge" logo (on promisetracker.ng) | Inline `data:image/svg+xml` in `https://promisetracker.ng/assets/index-CxWRBiNo.js` (`img src:uE alt:"Advokc"`) | 214×61. The maroon wordmark sits in a white box with a black offset "shadow" (fills `#77121A`, `white`, `black`, stroke `#231F20`). As a `data:` URI it would be blocked by our `img-src 'self'`, so it would have to be saved to a file. |
| Promise Tracker favicon | `https://promisetracker.ng/Promisefavicon.svg` | 32×32 chevron/hexagon mark. Fills **#BB3C2E** (front hexagon) and **#77121A** (three chevrons). It is a good basis for a dashboard favicon. |
| AdvoKC favicon | `https://cdn.prod.website-files.com/615ce181227811348a82a252/681091ed59e3cfb1eb6aee97_Vector.jpg` | 32×32 JPEG |
| (Ignore) | `…/69ba569dedbc5a63bfe0f1e7_…_untitled-ui-logo.png` | A leftover "Untitled UI" template placeholder in the navbar markup, not AdvoKC's logo |

Can we vendor it? Technically yes: the SVG is clean, and an inline `<svg>` or a `/static/` file satisfies the CSP. The dashboard is AdvoKC's own internal tool, but the wordmark is still their trademark, so **ask AdvoKC comms for the official SVG and usage rules** before committing it. Until then, the existing `/static/favicon.svg` and the text mark are fine.

### 1.5 Visual tone

- Both sites are built on one deep **maroon (#77121A)** with lots of white and near-black text (#231F20 / #0C1821). A pink tint (#FADCDC) is used for hover and selected states.
- The tracker adds navy (#1B3A50) and a muted grey (#5C6165), and uses a **serif display face (Bodoni)** for headlines over **Helvetica** body text. That gives it an editorial, newspaper feel.
- Buttons are solid maroon with white text and an 8px radius. Hover goes slightly lighter (#842930, AdvoKC) or darker (#5F1016, tracker).
- The tone is civic and serious rather than playful. The bright red (#D63321) and yellow (#FFD426, `--pledge-yellow`) are kept for campaign CTAs.

### 1.6 Contrast checks

Ratios are WCAG 2.x relative luminance ([Understanding 1.4.3](https://www.w3.org/WAI/WCAG21/Understanding/contrast-minimum.html): 4.5:1 for text, 3:1 for large text; [1.4.11](https://www.w3.org/WAI/WCAG21/Understanding/non-text-contrast.html): 3:1 for UI and focus indicators). They were computed with the standard formula in a local script.

Brand colours as published:

| Pair | Ratio | Result |
|---|---|---|
| #FFFFFF on #77121A (primary button) | 11.14 | AA |
| #FFFFFF on #842930 (AdvoKC hover) | 8.97 | AA |
| #FFFFFF on #5F1016 (tracker active) | 13.36 | AA |
| #77121A link on #FFFFFF | 11.14 | AA |
| #77121A on #FADCDC (selected nav) | 8.66 | AA |
| #BB3C2E on #FFFFFF | 5.53 | AA |
| #5C6165 muted on #FFFFFF | 6.26 | AA |
| #FFFFFF on #00BB03 (raw kept) | **2.59** | FAIL |
| #FFFFFF on #E20102 (raw broken) | 4.95 | AA (just) |
| #FFFFFF on #0073E6 (raw in works) | 4.57 | AA (just) |
| #00BB03 / #FAE916 / #D9D9D9 dot on white | 2.59 / 1.26 / 1.41 | FAIL as non-text |

The raw tracker hues work as **decorative dots next to a text label**, which satisfies 1.4.1 ([Use of Color](https://www.w3.org/WAI/WCAG21/Understanding/use-of-color.html)). They can't be the only signal, and white text can't sit on them. The derived palette below passes everywhere (section 3.1 lists the ratios).

---

## Part 2: UX patterns

### 2.1 App shell

- Navigation has **12 destinations in 3 groups** (Work: 3, Activity: 3, Admin: 6; `templates/base.html`). That is more than fits in a top bar. GOV.UK's service navigation suits "a small number" of links and wraps or collapses beyond that ([GOV.UK Service navigation](https://design-system.service.gov.uk/components/service-navigation/)). A **grouped left sidebar** is the right choice here, and it's what the current shell already does.
- On mobile (< ~900px), turn the sidebar into an off-canvas drawer behind a menu button with `aria-expanded` and `aria-controls`, following the [APG disclosure pattern](https://www.w3.org/WAI/ARIA/apg/patterns/disclosure/). Close it on Esc and on a scrim click, and return focus to the button.
- **Without JS**, the sidebar should render as a normal block at the top of the page (or behind a `<details>`), never hidden.
- Mark the current item with `aria-current="page"` and a visible non-colour cue as well as colour, such as a 3px bar or bold text. The template already sets `aria-current`.
- **Breadcrumbs** only on detail pages (Meter › Promise, Review › Article, Runs › Run #). Use `<nav aria-label="Breadcrumb"><ol>…`, with the current page as the last item. On mobile, collapse to the parent link only ([GOV.UK Breadcrumbs](https://design-system.service.gov.uk/components/breadcrumbs/)).

### 2.2 Data tables

- Use real `<table>` markup with `<caption>`, `<th scope="col">` and numeric columns right-aligned ([GOV.UK Table](https://design-system.service.gov.uk/components/table/)).
- **Sticky header**: `thead th { position: sticky; top: 0 }` inside a scroll container, with an opaque background ([MDN position](https://developer.mozilla.org/en-US/docs/Web/CSS/position)).
- **Mobile**: there are two options.
  1. Wrap the table in `<div class="table-wrap" tabindex="0" role="region" aria-label="…">` with `overflow-x:auto`, so the region is keyboard-scrollable.
  2. Show stacked cards, where each `<td data-label="Status">` shows its label through `td::before { content: attr(data-label) }` under a media query.

  Use cards for list pages (Review queue, Meters, Users) and scrolling for dense admin tables (Runs, LLM usage, Audit). Carbon's guidance is to keep tables scannable and reduce columns at small widths ([Carbon data table usage](https://carbondesignsystem.com/components/data-table/usage/)).
- **Sort and filter**: the server-side query string (`?sort=…&q=…`) is the baseline and works without JS. JS can add instant client-side filtering, which hides rows whose `textContent` doesn't match, and column sorting by putting a `<button>` inside each `<th>` and updating `aria-sort` ([APG sortable table](https://www.w3.org/WAI/ARIA/apg/patterns/table/examples/sortable-table/)). Announce "N of M rows shown" in a polite live region.
- **Empty states** should say what the list holds, why it's empty ("No articles waiting — the 08:00 run drafted none"), and the next action, if there is one ([Primer empty states](https://primer.style/product/ui-patterns/empty-states/)). Use a different message when filters hide everything: "No matches for 'x' · Clear filters".

### 2.3 Editorial review workflow

- **Side by side** on wide screens: a markdown textarea on the left and a rendered preview on the right, which is the same rendering the published site uses. Stack them on narrow screens. Without JS, a "Preview" submit button re-renders the page server-side.
- **Word counter**: modelled on [GOV.UK Character count](https://design-system.service.gov.uk/components/character-count/).
  - Show a static hint first ("60–100 words; anchor text counts, URLs don't"). JS then upgrades it to a live "87 / 100 words" count.
  - Announce the count through a **debounced** `aria-live="polite"` element, not on every keystroke.
  - Show an error style past 100.
  - The JS count must mirror `domain/qa.py` (PRD §3.6). The server QA re-check stays authoritative.
- **Pre-flight checklist** next to the editor: ≥2 links, closing line present, no "Promise Tracker", every URL in the citable set. These are the same rules as `qa.py`, shown before approval. The approval screen works like a "check answers" page ([GOV.UK Check answers](https://design-system.service.gov.uk/patterns/check-answers/)).
- **Keyboard shortcuts** (the shell already has a `?` dialog):
  - `j`/`k` next/previous item, `e` edit, `a` approve, `r` reject, `/` focus filter, `?` help.
  - Disable them while focus is in a text field, and never bind a destructive action to a single key without confirmation.
  - List them in the `?` dialog ([Primer keyboard shortcuts](https://primer.style/product/ui-patterns/keyboard-shortcuts/)).
- **Destructive actions** (reject, dismiss, delete user, revoke assignment) are normal POST forms.
  - Without JS, they go through a server confirmation page.
  - With JS, a native `<dialog>` asks to confirm, with focus starting on Cancel ([MDN dialog](https://developer.mozilla.org/en-US/docs/Web/HTML/Element/dialog)).
  - The confirm button names the action ("Reject article"), not "OK".
- **Flash messages / toasts** follow Post/Redirect/Get, with the server rendering the flash on the next page.
  - Use `role="status"` (polite) for success and `role="alert"` only for errors ([WCAG 4.1.3 Status messages](https://www.w3.org/WAI/WCAG21/Understanding/status-messages.html)).
  - Leave errors in place, don't auto-dismiss them. Success toasts may fade after ≥5s but keep a close button.
  - For page-level outcomes, GOV.UK's notification banner is the reference ([Notification banner](https://design-system.service.gov.uk/components/notification-banner/)).
- **Optimistic UI vs. form POST**: the current CSP has no `connect-src`, so `fetch()` is blocked anyway. Keep **form POST + redirect** for every write: approval is audited and must be confirmed by the server. Optimistic updates aren't worth the cost here.

### 2.4 Status badges

- Always pair **text + colour** (and optionally a dot or icon shape), e.g. `<span class="badge badge--kept"><span class="dot" aria-hidden="true"></span>Kept</span>` ([Use of Color](https://www.w3.org/WAI/WCAG21/Understanding/use-of-color.html); [Carbon status indicators](https://carbondesignsystem.com/patterns/status-indicator-pattern/); [GOV.UK Tag](https://design-system.service.gov.uk/components/tag/)).
- Use the tracker's public names (Kept, Broken, In the works, Stalled, Compromised, Not rated) so staff see the same labels as the public site.
- Keep the badge families separate: **promise ratings** (6), **pipeline states** (queued, verified, needs_sources, drafted, approved, published, stale), and **run outcomes** (ok, partial, failed).

### 2.5 Accessibility

- **Focus**: `:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px }`. The ring colour needs ≥3:1 against adjacent colours ([MDN :focus-visible](https://developer.mozilla.org/en-US/docs/Web/CSS/:focus-visible); [WCAG 1.4.11](https://www.w3.org/WAI/WCAG21/Understanding/non-text-contrast.html)).
- **Skip link**: make it the first focusable element and keep it visually hidden until it gets focus ([GOV.UK Skip link](https://design-system.service.gov.uk/components/skip-link/)). It already exists (`a.skip`).
- **Reduced motion**: `@media (prefers-reduced-motion: reduce) { *,*::before,*::after { transition-duration:.01ms!important; animation-duration:.01ms!important; scroll-behavior:auto!important } }` ([MDN](https://developer.mozilla.org/en-US/docs/Web/CSS/@media/prefers-reduced-motion)).
- **Forced colours / Windows High Contrast**: give badges a 1px `border` (it becomes visible) and avoid conveying state with background colour alone ([MDN forced-colors](https://developer.mozilla.org/en-US/docs/Web/CSS/@media/forced-colors)).
- Use `aria-live` for toasts, the word counter and the filter result count. Every form field needs a `<label>`, and errors should be linked with `aria-describedby`.

### 2.6 Dark mode under a strict CSP

- **CSS**: put light tokens on `:root`, and dark tokens under `@media (prefers-color-scheme: dark) { :root:not([data-theme=light]) {…} }` plus `:root[data-theme=dark] {…}`. Also set `color-scheme: light dark` (and `light` / `dark` on the forced attributes) so form controls and scrollbars match ([MDN prefers-color-scheme](https://developer.mozilla.org/en-US/docs/Web/CSS/@media/prefers-color-scheme), [MDN color-scheme](https://developer.mozilla.org/en-US/docs/Web/CSS/color-scheme), [web.dev prefers-color-scheme](https://web.dev/articles/prefers-color-scheme)). `light-dark()` is an option for single declarations ([MDN light-dark()](https://developer.mozilla.org/en-US/docs/Web/CSS/color_value/light-dark)).
- **Avoiding the flash without inline script** (inline is forbidden by `script-src 'self'`). There are two compatible options:
  1. *Blocking external head script* (what `dashboard.js` does today). `<script src="/static/dashboard.js">` without `defer` or `async` in `<head>` reads `localStorage` and sets `data-theme` before the body is parsed. It's allowed under `script-src 'self'`, and the cost is one cached request. web.dev's theme-switch article uses the same "run before paint" idea ([web.dev theme switch](https://web.dev/articles/building/a-theme-switch-component)).
  2. *Cookie + server render* (more robust). The toggle also writes a `pm_theme=light|dark` cookie (`SameSite=Lax`, not a secret), and Jinja renders `<html data-theme="{{ theme }}">`. There is no flash even before JS runs, and a no-JS `<form method=post action=/prefs/theme>` can set it.

  Recommendation: use option 2, and keep localStorage only as a mirror. Then the main script can move to `defer`.
- Make the toggle a `<button aria-pressed>` with three states (System, Light, Dark), or a button plus a "use system" reset. Hide it when JS is off (the `hidden` attribute is removed by JS).

### 2.7 Progressive enhancement

GOV.UK's service manual says to build the core HTML so it works first and treat JS as an enhancement ([Using progressive enhancement](https://www.gov.uk/service-manual/technology/using-progressive-enhancement)). Here that means:
- Every action is a real `<form method=post>` with CSRF.
- Every filter is a GET form.
- Every disclosure is `<details>` or server-rendered.

JS hooks in through `data-*` attributes from one deferred file. Use the `hidden` attribute for JS-only controls and remove it at startup.

### 2.8 Micro-interactions

- Durations: Carbon's productive motion uses roughly 70–150ms for small UI changes ([Carbon motion](https://carbondesignsystem.com/elements/motion/overview/)), and Material 3's "short" tokens run 50–200ms ([M3 easing and duration](https://m3.material.io/styles/motion/easing-and-duration/tokens-specs)). Keep **hover/focus at 120ms and drawers/toasts ≤200ms**, using `ease-out` on enter.
- Animate only `opacity` and `transform`, never layout properties.
- Skeletons are only worth it for content that loads after the page. Pages here are server-rendered, so prefer **instant pages + a pressed or busy state on submit**: disable the button and change its label to "Approving…" to stop double POSTs.
- Use subtle elevation for the sticky table header and the open drawer. Don't use parallax or confetti.

---

## Recommendations for this dashboard

### Token list

Derived from #77121A (primary), #5F1016 (pressed), #FADCDC (tint), #231F20 / #5C6165 (ink, muted), #BB3C2E (favicon red), and the tracker status hues. Every text pair was checked at ≥4.5:1 and every UI or focus pair at ≥3:1. Ratios are shown in comments.

```css
:root {                      /* LIGHT */
  --bg:#FAF7F7; --surface:#FFFFFF; --surface-2:#F4EEEE; --border:#E4D9DA;
  --border-strong:#8C7F81;   /* inputs: 3.84 vs #FFF */
  --ink:#231F20;             /* 15.30 on --bg */
  --muted:#5C6165;           /* 5.88 on --bg, 5.46 on --surface-2 */
  --brand:#77121A;           /* links 10.45 on --bg; #FFF on it 11.14 */
  --brand-hover:#5F1016;     /* #FFF on it 13.36 */
  --brand-ink:#FFFFFF;
  --brand-soft:#FADCDC;      /* selected nav; --brand on it 8.66 */
  --accent:#BB3C2E;          /* sparing highlights; 5.53 on #FFF */
  --focus:#BB3C2E;           /* 5.19 vs --bg */
  --navy:#0C1821;            /* optional dark header/footer band */
  --warn-bg:#FFF4E0; --warn:#9A5A00;   /* 5.02 */
  /* rating badges: text on tint; dot uses raw tracker hue */
  --kept-bg:#E3F7E3; --kept:#0A6B0C; --kept-dot:#00BB03;          /* 5.98 */
  --broken-bg:#FDE4E4; --broken:#A50F10; --broken-dot:#E20102;    /* 6.50 */
  --works-bg:#E1EFFC; --works:#0A5BB0; --works-dot:#0073E6;       /* 5.72 */
  --stalled-bg:#FDF8C4; --stalled:#6B5D00; --stalled-dot:#FAE916; /* 6.08 */
  --compr-bg:#FFECCC; --compr:#8A4B00; --compr-dot:#FFA500;       /* 5.87 */
  --unrated-bg:#ECECEC; --unrated:#4A4A4A; --unrated-dot:#9B9999; /* 7.50 */
  --radius:8px; --t-fast:120ms; --t-med:200ms;
}
/* DARK (applied under prefers-color-scheme and [data-theme=dark]) */
  --bg:#141012; --surface:#1D1719; --surface-2:#261F21; --border:#3A3033;
  --border-strong:#7A6C6F;   /* 3.53 vs --surface */
  --ink:#F2ECEC;             /* 16.16 */
  --muted:#B0A6A8;           /* 7.97 on --bg, 7.46 on --surface */
  --brand:#9B1C26;           /* button fill; #FFF on it 8.11 */
  --brand-hover:#B8323D;     /* #FFF on it 5.90 */
  --link:#F0A3AA;            /* 9.46 on --bg, 8.85 on --surface (use for links/active text) */
  --brand-soft:#3A1A1E;      /* --ink on it 13.36 */
  --focus:#F0A3AA;           /* 9.46 */
  --warn-bg:#3A2700; --warn:#FFC266;
  --kept-bg:#0F2E10; --kept:#7EE07F;        /* 9.06 */
  --broken-bg:#3A1112; --broken:#FF9B9B;    /* 8.21 */
  --works-bg:#0D2540; --works:#8CC4FF;      /* 8.45 */
  --stalled-bg:#332F05; --stalled:#F5E663;  /* 10.57 */
  --compr-bg:#3A2700; --compr:#FFC266;      /* 8.95 */
  --unrated-bg:#2A2A2A; --unrated:#CFCFCF;  /* 9.21 */
```

In light mode `--link` is the same as `--brand`. Font stack: `system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif`. Inter (OFL) is optional, and needs `font-src 'self'`.

Note that the current `dashboard.css` uses a green accent (#0B6E4F) and a **purple** "compromised" (#8A3EA8). Both conflict with the brand maroon and the tracker's orange compromised (#FFA500), so they should change.

### Layout decision

Keep the **grouped left sidebar** (Work / Activity / Admin), 240px wide on desktop, with a light surface. The active item gets a `--brand-soft` background, `--brand` text and a 3px `--brand` left bar. Below 900px it becomes an off-canvas drawer. Add a slim top bar with breadcrumbs, the theme toggle and the `?` hint. In the brand slot, put the AdvoKC wordmark SVG (inline, `fill="currentColor"`) once comms approves it, or a mark derived from the Promise Tracker favicon. Content max-width is about 1200px. Review pages use a two-column editor/preview grid.

### Top 10 enhancements

1. **Re-skin to the AdvoKC palette**: replace the green accent with maroon and switch "compromised" to orange, using the tokens above.
2. **Status badges** with tinted backgrounds, AA text, the tracker-hue dot, and a 1px border for forced-colours mode. Use separate families for ratings, pipeline states and run outcomes.
3. **Cookie-backed theme** rendered server-side into `<html data-theme>`, with localStorage as a mirror. That gives no flash, works without JS, and lets `dashboard.js` be `defer`.
4. **Review editor**: side-by-side textarea and server-identical preview, a live debounced word counter mirroring `qa.py`, and a QA checklist panel. Approve stays disabled while hard rules fail, and the server re-checks regardless.
5. **Confirm dialogs** (`<dialog>`) for reject, dismiss and delete, with a server confirmation page as the no-JS fallback. Submit buttons switch to a busy state so they can't post twice.
6. **Responsive tables**: card layout for list pages, a keyboard-focusable scroll region for dense admin tables, and sticky headers.
7. **Client-side filter and sort** layered on the server `?q=&sort=`, with `aria-sort` and a live "N of M shown" count.
8. **Flash/toast rules**: success uses `role=status` and auto-fades after ≥5s, errors use `role=alert` and stay. Use P/R/G everywhere.
9. **Empty states** that explain the cause and the next step, with a separate "no filter matches" variant.
10. **Accessibility polish**: a 3px `--focus` ring with offset, `prefers-reduced-motion` and `forced-colors` blocks, breadcrumbs on detail pages, and all motion ≤200ms on opacity and transform only.
