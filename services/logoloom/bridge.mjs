/**
 * LogoLoom, called directly instead of over MCP.
 *
 * Upstream (mcpware/logoloom, MIT) ships an MCP stdio server whose four tools
 * are thin wrappers over four local modules. Persei has no MCP client, and
 * adding one -- protocol, handshake, a long-lived subprocess per session --
 * to reach four pure functions in the same language would be machinery with
 * nothing on the other end of it. This imports the modules.
 *
 * Read JSON on argv[2], write JSON on stdout, exit non-zero on failure.
 * One process per call: these are seconds-long operations a handful of times
 * per task, not a hot loop.
 *
 * Three of the four are used as they are. The fourth, image-to-svg.mjs, is
 * not -- see imageToSvgSafely below. Short version: it shells out with the
 * caller's path interpolated into the command string, and it passes vtracer
 * 0.6's flag names to a vtracer that renamed them, so it fails on every input
 * anyway. Tracing is done here instead, with an argv array and no shell.
 */
import { execFileSync } from 'node:child_process';
import { accessSync, constants, copyFileSync, existsSync, mkdtempSync, readFileSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, extname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const MOD = '@mcpware/logoloom/src/tools/';

// vtracer lives in vendor/ next to this file rather than in /usr/local/bin:
// it is a dependency of one tool in one service, and putting it on the system
// PATH would make removing it somebody's archaeology later. Prepending here
// covers both the check below and upstream's own `execSync('vtracer ...')`,
// which inherits this process's environment.
const HERE = dirname(fileURLToPath(import.meta.url));
process.env.PATH = `${join(HERE, 'vendor')}:${process.env.PATH || ''}`;

function have(binary) {
  // Walked rather than shelled out to. `command -v` needs a shell, and Node
  // warns (correctly) that passing args with shell:true concatenates instead
  // of escaping them -- which is the very habit this file exists to undo.
  for (const dir of (process.env.PATH || '').split(':')) {
    if (!dir) continue;
    try {
      accessSync(join(dir, binary), constants.X_OK);
      return true;
    } catch {
      // not here; keep looking
    }
  }
  return false;
}

async function imageToSvgSafely({ imagePath, colorMode, precision }) {
  const mode = colorMode === 'binary' ? 'binary' : 'color';
  if (!have('vtracer')) {
    return {
      success: false,
      error:
        'no vectorizer installed. vtracer lives in services/logoloom/vendor/ -- ' +
        'reinstall it there, or `cargo install vtracer`.',
    };
  }

  const st = statSync(imagePath);        // throws for a missing file; caught by the caller
  if (!st.isFile()) throw new Error(`${imagePath} is not a file`);

  // Upstream's own imageToSvg is not used for this. It invokes vtracer through
  // `execSync` with a template string -- a shell, and a model-chosen path
  // interpolated into it -- and it passes vtracer 0.6's flag names
  // (--colormode, --filter_speckle), which 1.0 renamed, so every call fails
  // whatever the path. Calling vtracer here with an argv array fixes the
  // flags and removes the shell, which is a stronger guarantee than escaping
  // one: with no shell there is nothing for a filename to be interpreted as.
  //
  // The copy stays anyway. It costs three lines and it means no string the
  // model chose reaches the subprocess at all, whatever anyone changes
  // downstream, plus it resolves a symlink before the tracer follows it.
  const dir = mkdtempSync(join(tmpdir(), 'logoloom-in-'));
  const ext = /^\.[a-z0-9]{1,5}$/i.test(extname(imagePath)) ? extname(imagePath).toLowerCase() : '.png';
  const src = join(dir, `input${ext}`);
  const out = join(dir, 'out.svg');
  try {
    copyFileSync(imagePath, src);
    const args = [
      '--input', src,
      '--output', out,
      '--clustering', mode === 'binary' ? 'bw' : 'color-cluster',
      '--filter-speckle', '4',
      '--color-precision', '6',
      '--path-precision', String(Math.min(10, Math.max(1, precision || 6))),
    ];
    try {
      execFileSync('vtracer', args, { timeout: 60000, stdio: 'pipe' });
    } catch (e) {
      const why = (e.stderr && e.stderr.toString().trim()) || e.message;
      return { success: false, error: `vtracer failed: ${why.slice(0, 300)}` };
    }
    if (!existsSync(out)) return { success: false, error: 'vectorization produced no output' };
    const svg = readFileSync(out, 'utf-8');
    return { success: true, fileSize: Buffer.byteLength(svg, 'utf8'), svg };
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}


async function run(op, args) {
  switch (op) {
    case 'text_to_path': {
      const { textToPath } = await import(MOD + 'text-to-path.mjs');
      return JSON.parse(await textToPath(args.svg, args.fontPath));
    }
    case 'optimize_svg': {
      const { optimizeSvg } = await import(MOD + 'optimize-svg.mjs');
      return JSON.parse(await optimizeSvg(args.svg, Boolean(args.aggressive)));
    }
    case 'export_brand_kit': {
      const { exportBrandKit } = await import(MOD + 'export-brand-kit.mjs');
      return JSON.parse(await exportBrandKit(args));
    }
    case 'image_to_svg':
      return await imageToSvgSafely(args);
    case 'render_png': {
      // Not upstream's. The model writes SVG it cannot see, which is the same
      // gap preview_app closed for a running page: sharp is already here to
      // rasterise, and the caller turns this into something it can look at.
      const sharp = (await import('sharp')).default;
      // `background` goes BEHIND the logo, via flatten. It used to be passed
      // only to resize(), where it fills the letterbox padding and nothing
      // else -- so with a logo already the target's shape, every "how does it
      // look on dark / on white" render came back transparent and the vision
      // model judged it on a background of its own choosing. Found 2026-09-23:
      // a wordmark all but invisible on dark was described as legible.
      let img = sharp(Buffer.from(args.svg, 'utf8'), { density: args.density || 144 })
        .resize({
          width: args.width || 512,
          height: args.height || 512,
          fit: 'contain',
          background: { r: 0, g: 0, b: 0, alpha: 0 },
        });
      if (args.background) img = img.flatten({ background: args.background });
      const png = await img.png().toBuffer();
      return { success: true, pngBase64: png.toString('base64'), bytes: png.length };
    }
    default:
      return { success: false, error: `unknown operation ${op}` };
  }
}

const payload = JSON.parse(process.argv[2] || '{}');
try {
  const result = await run(payload.op, payload.args || {});
  process.stdout.write(JSON.stringify(result));
} catch (e) {
  process.stdout.write(JSON.stringify({ success: false, error: String(e && e.message || e) }));
  process.exit(1);
}
