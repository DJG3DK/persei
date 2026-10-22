#!/usr/bin/env node
// Write a release's updater manifest (latest.json) with the Windows installer.
//
//   node scripts/write_windows_updater_manifest.mjs <latest.json> <dir> <owner/repo> <tag>
//
// tauri-action wrote this file until 2026-10-22, from a build that had the
// updater's signing key in its environment, along with every tool the build
// runs (release.yml, I24). The build now runs without the key, a step of its
// own signs the installer with `tauri signer sign`, and this writes what
// tauri-action wrote: `windows-x86_64` and the bundle-specific
// `windows-x86_64-nsis` the updater tries first, both the installer in <dir>
// with the signature from its .sig. The Linux entries are added later, by
// add_linux_to_updater_manifest.mjs in the publish job. Exactly one
// installer, with a signature, or it fails: a manifest pointing at nothing
// would break every Windows app's update check.
import fs from "node:fs";
import path from "node:path";

const [manifestPath, dir, repo, tag] = process.argv.slice(2);
if (!manifestPath || !dir || !repo || !tag) {
  console.error("usage: write_windows_updater_manifest.mjs <latest.json> <dir> <owner/repo> <tag>");
  process.exit(2);
}
if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo) || !/^v[0-9A-Za-z.-]+$/.test(tag)) {
  console.error("unexpected repository or tag");
  process.exit(2);
}
const installers = fs.readdirSync(dir).filter((f) => f.endsWith("_x64-setup.exe"));
if (installers.length !== 1) {
  console.error(`expected one installer in ${dir}, found ${installers.length}`);
  process.exit(1);
}
const exe = installers[0];
const sigPath = path.join(dir, `${exe}.sig`);
const signature = fs.existsSync(sigPath) ? fs.readFileSync(sigPath, "utf8").trim() : "";
if (!signature) {
  console.error(`${exe} has no signature (${exe}.sig)`);
  process.exit(1);
}
const entry = {
  signature,
  url: `https://github.com/${repo}/releases/download/${tag}/${encodeURIComponent(exe)}`,
};
// The app's version is the tag's (release.yml, "Version the app after the tag").
const manifest = {
  version: tag.slice(1),
  notes: "See CHANGELOG.md.",
  pub_date: new Date().toISOString(),
  platforms: { "windows-x86_64": entry, "windows-x86_64-nsis": entry },
};
fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2) + "\n");
console.log(`latest.json: windows-x86_64 -> ${entry.url}`);
