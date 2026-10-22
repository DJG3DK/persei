#!/usr/bin/env node
// Add the Linux AppImage to a release's updater manifest (latest.json).
//
//   node scripts/add_linux_to_updater_manifest.mjs <latest.json> <dir> <owner/repo> <tag>
//
// The Windows job writes latest.json (write_windows_updater_manifest.mjs);
// the Linux build only builds, so the two jobs never race over the file
// (release.yml, publish).
// This adds the entry Tauri's updater looks up on Linux: `linux-x86_64`,
// and the bundle-specific `linux-x86_64-appimage` it tries first, both the
// AppImage in <dir> with the signature from its .sig. Nothing else in the
// manifest changes. Exactly one AppImage, with a signature, or it fails:
// a release whose Linux entry points at nothing would break every
// AppImage's update check.
import fs from "node:fs";
import path from "node:path";

const [manifestPath, dir, repo, tag] = process.argv.slice(2);
if (!manifestPath || !dir || !repo || !tag) {
  console.error("usage: add_linux_to_updater_manifest.mjs <latest.json> <dir> <owner/repo> <tag>");
  process.exit(2);
}
if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo) || !/^v[0-9A-Za-z.-]+$/.test(tag)) {
  console.error("unexpected repository or tag");
  process.exit(2);
}
const images = fs.readdirSync(dir).filter((f) => f.endsWith(".AppImage"));
if (images.length !== 1) {
  console.error(`expected one AppImage in ${dir}, found ${images.length}`);
  process.exit(1);
}
const image = images[0];
const sigPath = path.join(dir, `${image}.sig`);
const signature = fs.existsSync(sigPath) ? fs.readFileSync(sigPath, "utf8").trim() : "";
if (!signature) {
  console.error(`${image} has no signature (${image}.sig)`);
  process.exit(1);
}
const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
manifest.platforms = manifest.platforms || {};
const entry = {
  signature,
  url: `https://github.com/${repo}/releases/download/${tag}/${encodeURIComponent(image)}`,
};
manifest.platforms["linux-x86_64"] = entry;
manifest.platforms["linux-x86_64-appimage"] = entry;
fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2) + "\n");
console.log(`latest.json: linux-x86_64 -> ${entry.url}`);
