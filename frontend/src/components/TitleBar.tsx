/* oxlint-disable react/only-export-components -- insideDesktopApp() is the one non-component export, and App reads it */
import { useEffect, useState } from "react";
import "./TitleBar.css";

/* Inside the desktop app the dashboard window is frameless, so this strip is
   its title bar: a drag handle and the three window controls. Outside the
   app (a browser tab) it renders nothing. The app injects window.__TAURI__
   into this page; what the page may call is app/src-tauri/capabilities/
   console.json: the window controls here, and open_panel. */
type TauriWindow = {
  minimize: () => Promise<void>;
  toggleMaximize: () => Promise<void>;
  close: () => Promise<void>;
};
type Tauri = { window?: { getCurrentWindow?: () => TauriWindow }; core?: { invoke?: (cmd: string) => Promise<unknown> } };

function tauri(): Tauri | undefined {
  return (window as unknown as { __TAURI__?: Tauri }).__TAURI__;
}

function currentWindow(): TauriWindow | null {
  try {
    return tauri()?.window?.getCurrentWindow?.() ?? null;
  } catch {
    return null;
  }
}

/* One window: the console and the app's control panel take turns in it.
   This asks the app to show the panel. */
function openPanel() {
  void tauri()?.core?.invoke?.("open_panel");
}

export function insideDesktopApp(): boolean {
  return currentWindow() !== null;
}

export function TitleBar() {
  // Read once: the app injects window.__TAURI__ before any script runs, so
  // the answer cannot change after the first render.
  const [win] = useState<TauriWindow | null>(() => currentWindow());
  useEffect(() => {
    if (!win) return;
    document.documentElement.classList.add("in-desktop-app");
    return () => document.documentElement.classList.remove("in-desktop-app");
  }, [win]);
  if (!win) return null;
  return (
    <div className="titlebar" data-tauri-drag-region data-testid="titlebar">
      <span className="titlebar-name" data-tauri-drag-region>Persei</span>
      <button type="button" className="titlebar-link" onClick={openPanel}>Control panel</button>
      <span className="titlebar-controls">
        <button type="button" className="titlebar-btn" aria-label="Minimise" onClick={() => void win.minimize()}>&#x2500;</button>
        <button type="button" className="titlebar-btn" aria-label="Maximise" onClick={() => void win.toggleMaximize()}>&#x25A1;</button>
        <button type="button" className="titlebar-btn titlebar-btn--close" aria-label="Close" onClick={() => void win.close()}>&#x2715;</button>
      </span>
    </div>
  );
}
