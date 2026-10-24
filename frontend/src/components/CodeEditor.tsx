/* Monaco -- the editor inside VS Code -- as a side-by-side diff with the
 * right-hand side editable. Imported lazily (DiffPanel), so its several
 * megabytes load only when an operator actually opens a file to edit.
 *
 * Bundled, never fetched from a CDN: the dashboard's CSP is script-src
 * 'self', and the common React wrapper's default is to pull Monaco from
 * jsdelivr at runtime. Workers are Vite `?worker` imports, emitted as
 * same-origin files, which that CSP already allows.
 *
 * DOMPurify: Monaco vendors DOMPurify 3.4.15 (esm/vs/base/browser/dompurify/),
 * which no npm override reaches (audit U25), so vite.config.ts redirects
 * Monaco's one import of it to the npm package, pinned at a patched
 * release. CodeEditor.test.ts fails if Monaco's vendored version or that
 * import changes, so this note cannot go stale. Drop the redirect once
 * Monaco vendors a release at or above the npm one.
 */
import { useEffect, useRef } from "react";
import * as monaco from "monaco-editor";
import EditorWorker from "monaco-editor/editor/editor.worker?worker";
import TsWorker from "monaco-editor/languages/features/typescript/ts.worker?worker";
import JsonWorker from "monaco-editor/languages/features/json/json.worker?worker";
import CssWorker from "monaco-editor/languages/features/css/css.worker?worker";
import HtmlWorker from "monaco-editor/languages/features/html/html.worker?worker";

self.MonacoEnvironment = {
  getWorker(_id: string, label: string) {
    if (label === "typescript" || label === "javascript") return new TsWorker();
    if (label === "json") return new JsonWorker();
    if (label === "css" || label === "scss" || label === "less") return new CssWorker();
    if (label === "html" || label === "handlebars" || label === "razor") return new HtmlWorker();
    return new EditorWorker();
  },
};

// Plain JS files get the same completions and error squiggles as TS, which
// is most of what "autocomplete" means to someone fixing a line by hand.
monaco.typescript.javascriptDefaults.setCompilerOptions({
  allowJs: true,
  checkJs: false,
  target: monaco.typescript.ScriptTarget.ES2020,
  module: monaco.typescript.ModuleKind.ESNext,
  jsx: monaco.typescript.JsxEmit.React,
});
monaco.typescript.typescriptDefaults.setCompilerOptions({
  target: monaco.typescript.ScriptTarget.ES2020,
  module: monaco.typescript.ModuleKind.ESNext,
  moduleResolution: monaco.typescript.ModuleResolutionKind.NodeJs,
  jsx: monaco.typescript.JsxEmit.ReactJSX,
  allowNonTsExtensions: true,
  esModuleInterop: true,
});
// A single file, out of its project: imports of the project's own modules
// cannot resolve here, and flagging every one would bury real mistakes.
monaco.typescript.typescriptDefaults.setDiagnosticsOptions({
  noSemanticValidation: false,
  noSyntaxValidation: false,
  diagnosticCodesToIgnore: [2307, 2792, 7016],
});

const EXT_LANG: Record<string, string> = {
  ts: "typescript", tsx: "typescript", mts: "typescript", cts: "typescript",
  js: "javascript", jsx: "javascript", mjs: "javascript", cjs: "javascript",
  json: "json", css: "css", scss: "scss", less: "less", html: "html", htm: "html",
  md: "markdown", py: "python", rb: "ruby", go: "go", rs: "rust", java: "java",
  php: "php", sh: "shell", bash: "shell", yml: "yaml", yaml: "yaml", sql: "sql",
  xml: "xml", svg: "xml", vue: "html", c: "c", h: "c", cpp: "cpp", cs: "csharp",
  kt: "kotlin", swift: "swift", dockerfile: "dockerfile", toml: "ini", ini: "ini",
};

function languageFor(path: string): string {
  const name = path.split("/").pop()?.toLowerCase() ?? "";
  if (name === "dockerfile") return "dockerfile";
  return EXT_LANG[name.split(".").pop() ?? ""] ?? "plaintext";
}

interface Props {
  path: string;
  original: string;
  modified: string;
  onChange: (value: string) => void;
}

export default function CodeEditor({ path, original, modified, onChange }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  useEffect(() => {
    if (!host.current) return;
    const language = languageFor(path);
    // Distinct URIs per side: the TypeScript service keys models by URI, and
    // two models at one URI is an error.
    const orig = monaco.editor.createModel(original, language, monaco.Uri.parse(`original:///${path}`));
    const mod = monaco.editor.createModel(modified, language, monaco.Uri.parse(`file:///${path}`));
    const narrow = window.matchMedia("(max-width: 800px)").matches;
    const editor = monaco.editor.createDiffEditor(host.current, {
      theme: "vs-dark",
      automaticLayout: true,
      originalEditable: false,
      readOnly: false,
      renderSideBySide: !narrow,
      useInlineViewWhenSpaceIsLimited: true,
      minimap: { enabled: false },
      fontSize: 13,
      scrollBeyondLastLine: false,
      renderOverviewRuler: false,
    });
    mod.updateOptions({ tabSize: 2 });
    editor.setModel({ original: orig, modified: mod });
    const sub = mod.onDidChangeContent(() => onChangeRef.current(mod.getValue()));
    editor.getModifiedEditor().focus();
    return () => {
      sub.dispose();
      editor.dispose();
      orig.dispose();
      mod.dispose();
    };
    // A new file is a new editor; later edits flow out through onChange and
    // must not be pushed back in, or every keystroke would reset the cursor.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);

  return <div className="code-editor" ref={host} />;
}
