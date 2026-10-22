/* Registering public/sw.js, and nothing else.
 *
 * Production only. In `vite dev` a service worker would serve yesterday's
 * module graph over HMR and make every change look like it did nothing --
 * the classic "why is my edit not showing" afternoon.
 *
 * `import.meta.env.PROD` rather than a hostname check: the dev server and the
 * built app are the real distinction, and a preview build on localhost should
 * behave like production because that is what it is there to prove.
 */

/** Ask the waiting worker to take over, then reload once it has. */
function activateUpdate(reg: ServiceWorkerRegistration): void {
  const waiting = reg.waiting;
  if (!waiting) return;
  // controllerchange fires once the new worker controls the page. Reloading
  // then (rather than immediately after postMessage) means the reload is
  // served by the new worker instead of racing it.
  let reloaded = false;
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (reloaded) return;
    reloaded = true;
    window.location.reload();
  });
  waiting.postMessage('skip-waiting');
}

export function registerServiceWorker(): void {
  if (!import.meta.env.PROD) return;
  if (!('serviceWorker' in navigator)) return;

  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).then((reg) => {
      // A worker that arrived while the app was closed is already waiting;
      // take it now, before the operator starts reading a stale shell.
      if (reg.waiting && navigator.serviceWorker.controller) activateUpdate(reg);

      reg.addEventListener('updatefound', () => {
        const next = reg.installing;
        if (!next) return;
        next.addEventListener('statechange', () => {
          // `controller` is null on the very first install -- there is no
          // previous worker, nothing is stale, and reloading would bounce
          // the app for no reason.
          if (next.state === 'installed' && navigator.serviceWorker.controller) activateUpdate(reg);
        });
      });

      // The dashboard is left open for days at a time, so it would otherwise
      // only notice a deploy when the tab was reopened.
      setInterval(() => { reg.update().catch(() => undefined); }, 60 * 60 * 1000);
    }).catch(() => {
      // An unregistrable worker costs the install prompt and nothing else;
      // the app itself works exactly as before, so this is not worth an
      // error in front of the operator.
    });
  });
}

/** The active worker's registration, or a clear error when there is none.
 *
 * `navigator.serviceWorker.ready` never settles on a page no worker
 * registered -- `vite dev`, a browser that refused the registration -- so
 * "Enable notifications" sat on its busy state forever until 2026-10-22
 * (audit U29). No registration at all fails at once; one still installing
 * gets `timeoutMs` to activate. */
export async function serviceWorkerReady(timeoutMs = 10_000): Promise<ServiceWorkerRegistration> {
  const sw = navigator.serviceWorker;
  if (!(await sw.getRegistration())) {
    throw new Error("No service worker is running on this page, so it cannot receive notifications. Reload the console and try again.");
  }
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error("The service worker did not start. Reload the console and try again.")), timeoutMs);
  });
  try {
    return await Promise.race([sw.ready, timeout]);
  } finally {
    clearTimeout(timer);
  }
}
