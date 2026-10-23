/* The build entry. It renders nothing.
 *
 * Its job is to put LandingPage into the bundler's module graph so Vite emits
 * the stylesheet and content-hashes the screenshots and the logo the
 * component imports. Importing only the CSS was not enough -- the images are
 * imported by the COMPONENT, so without it in the graph they were never
 * emitted and the rendered markup pointed at files the build had not made.
 *
 * Exported rather than merely imported because an entry chunk's exports are
 * roots: a bare import with no use is free for Rollup to tree-shake, taking
 * the assets with it.
 *
 * The page's HTML is produced at build time by scripts/render.mjs, which then
 * strips this module's <script> tag -- so none of this reaches a browser. The
 * deployed site is HTML, CSS and images.
 */
import "./tokens.css";
import { LandingPage } from "./LandingPage";

export default LandingPage;
