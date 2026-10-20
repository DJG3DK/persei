/* The colour schemes, and the two functions that apply one.
 *
 * The single source of truth for WHICH schemes exist is this list plus the
 * matching [data-theme] blocks in theme.css; agent/auth.py's THEMES is the
 * server-side copy of the same set and tests/test_themes.py holds the three
 * in step. A scheme in one place and not another is the failure worth
 * preventing: the server would accept a value the stylesheet has no rule for,
 * and the operator would save a colour and watch nothing change.
 *
 * The swatches are duplicated from theme.css on purpose. A preview tile has
 * to paint a scheme it is NOT applying -- five of them side by side, each in
 * its own colours -- and CSS custom properties cannot be read out of a
 * stylesheet for a selector that is not in effect.
 */

export type ThemeId = 'drafting' | 'indigo' | 'orchid' | 'ember' | 'moss';

interface Theme {
  id: ThemeId;
  label: string;
  /** One line, in the Settings list. What it feels like, not what it is. */
  blurb: string;
  /** For the tile: [ground, surface, accent, text]. */
  swatch: [string, string, string, string];
}

export const THEMES: Theme[] = [
  {
    id: 'drafting',
    label: 'Drafting',
    blurb: 'Brass on blue-graphite. The original, and the mark it was drawn for.',
    swatch: ['#11151a', '#1b2028', '#c9a227', '#edf0f4'],
  },
  {
    id: 'indigo',
    label: 'Indigo',
    blurb: 'Cool and quiet. The least shouty of the five on a long night.',
    swatch: ['#0e1018', '#191d2b', '#8b93f8', '#eceef6'],
  },
  {
    id: 'orchid',
    label: 'Orchid',
    blurb: 'Violet on a warm-dark ground. A callback to the original look.',
    swatch: ['#140f18', '#211a27', '#d879d8', '#f2ecf5'],
  },
  {
    id: 'ember',
    label: 'Ember',
    blurb: 'Warm charcoal and a low orange. Easiest of the five after dark.',
    swatch: ['#161210', '#241d19', '#f0913c', '#f5efe9'],
  },
  {
    id: 'moss',
    label: 'Moss',
    blurb: 'Green-graphite with a bright lime. The highest-contrast accent here.',
    swatch: ['#0f1411', '#19211b', '#9ad13f', '#ecf2ed'],
  },
];

const DEFAULT_THEME: ThemeId = 'drafting';

const STORAGE_KEY = 'tektonix.theme';

export function isThemeId(v: unknown): v is ThemeId {
  return typeof v === 'string' && THEMES.some((t) => t.id === v);
}

/** Paint a scheme on the document. Drafting is :root, so it carries no
 *  attribute -- that keeps the default free of a selector and means an
 *  attribute that somehow went missing lands on the default rather than on
 *  nothing. */
export function applyTheme(id: ThemeId): void {
  const root = document.documentElement;
  if (id === DEFAULT_THEME) root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', id);
  // The installed app's status bar and the browser's address bar read this.
  // Without it, picking Ember leaves an Android phone with a blue-graphite
  // bar above a warm-charcoal app.
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) {
    const t = THEMES.find((x) => x.id === id);
    if (t) meta.setAttribute('content', t.swatch[0]);
  }
  try {
    localStorage.setItem(STORAGE_KEY, id);
  } catch {
    // Private windows and blocked site data throw here. The preference still
    // works for this session and still persists on the account; all that is
    // lost is the no-flash start below.
  }
}

/** The scheme to paint BEFORE the server has answered.
 *
 * /api/auth/me is a round trip, and React renders long before it lands, so
 * reading the account's real answer first would show every operator who is
 * not on Drafting a flash of Drafting on every load. The account remains the
 * source of truth: this is the last one this browser saw, and App reconciles
 * it as soon as the real value arrives.
 */
export function storedTheme(): ThemeId {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (isThemeId(v)) return v;
  } catch {
    // see applyTheme
  }
  return DEFAULT_THEME;
}
