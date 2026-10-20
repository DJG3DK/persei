/* The Persei mark on its own: the gold diamond P, without the wordmark or
 * the constellation (scripts/brand/persei.py draws the same gem).
 *
 * Why a drawn SVG rather than the favicon or the logo PNG. The favicon is a
 * rounded tile with its own navy background baked in -- on the dashboard's
 * dark chat surface that reads as a dark square with a gem on it. The logo
 * PNG is the full lockup, mark plus wordmark, and cropping a 32px avatar out
 * of it wastes most of the file on type nobody can read at that size.
 *
 * Drawn, it floats: no tile, no plate, no circle, just the gem against
 * whatever is behind it. It also stays crisp at 32px on a retina display,
 * which a 32px PNG does not.
 *
 * The colours are the brand's, not the theme's. `--accent` is per-theme (gold,
 * periwinkle, orchid) and following it would recolour the gem to match
 * whichever theme is on -- cohesive, and no longer the Persei mark. The
 * sidebar lockup is fixed gold for the same reason, and these two sit on the
 * same screen.
 */

const GOLD = "#EFBF4E";
const NAVY = "#0B1426";

export function PerseiMark({ size = 24, className = "" }: { size?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      // Decorative: the name is already beside it in text, and a screen
      // reader announcing "Persei logo, Agent" is worse than "Agent".
      aria-hidden="true"
      focusable="false"
    >
      {/* The gem: a rounded square turned 45 degrees. */}
      <rect x="3.8" y="3.8" width="16.4" height="16.4" rx="1.97" fill={GOLD} transform="rotate(45 12 12)" />
      {/* The P, cut from it in the navy of the tiles. */}
      <path
        d="M9.44 7.71H12.45A3.02 3.02 0 0 1 12.45 13.74H11.4V16.76H9.44ZM11.4 9.52H12.38A1.21 1.21 0 0 1 12.38 11.93H11.4Z"
        fill={NAVY}
        fillRule="evenodd"
      />
    </svg>
  );
}
