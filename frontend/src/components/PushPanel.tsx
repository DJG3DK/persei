import { useCallback, useEffect, useState } from "react";
import { getPushKey, subscribePush, testPush, unsubscribePush } from "../api";
import { serviceWorkerReady } from "../registerServiceWorker";
import "./PushPanel.css";

/* Turning on notifications for THIS device.
 *
 * A push subscription belongs to a browser, not to an account: the same
 * operator legitimately has one on a phone and another on a laptop, and each
 * has to be granted separately because the permission is the OS's, not ours.
 * So everything here says "this device" and the count says how many others
 * the account has.
 *
 * The awkward cases are the whole job:
 *
 *   Permission "denied" is a dead end from JavaScript. The browser will never
 *   prompt again, and calling requestPermission() resolves "denied"
 *   immediately with no UI -- so a button that offers to try is a button that
 *   does nothing. It has to send the operator to site settings instead.
 *
 *   iOS only delivers web push to an app installed to the Home Screen, and
 *   only from iOS 16.4. In Safari's normal tab the APIs exist and subscribing
 *   appears to work, so the honest thing is to detect the tab case up front
 *   rather than let someone subscribe and wonder why nothing ever arrives.
 */

type Perm = "default" | "granted" | "denied" | "unsupported";

function isIos(): boolean {
  return /iPad|iPhone|iPod/.test(navigator.userAgent)
    // iPadOS reports as a Mac; the touch points are what give it away.
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
}

function isStandalone(): boolean {
  return window.matchMedia?.("(display-mode: standalone)").matches
    || (navigator as { standalone?: boolean }).standalone === true;
}

/** The base64url VAPID key the browser wants as raw bytes. */
function urlBase64ToUint8Array(b64: string): Uint8Array {
  const padded = (b64 + "=".repeat((4 - (b64.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(padded);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

function keyToB64(key: ArrayBuffer | null): string {
  if (!key) return "";
  const bytes = new Uint8Array(key);
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function PushPanel() {
  const supported = typeof window !== "undefined"
    && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

  const [perm, setPerm] = useState<Perm>(supported ? Notification.permission as Perm : "unsupported");
  const [subscribedHere, setSubscribedHere] = useState(false);
  const [devices, setDevices] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!supported) return;
    try {
      const reg = await serviceWorkerReady();
      setSubscribedHere(Boolean(await reg.pushManager.getSubscription()));
    } catch {
      setSubscribedHere(false);
    }
    try {
      setDevices((await getPushKey()).subscriptions);
    } catch {
      // The count is a nicety; failing to read it must not hide the toggle.
    }
  }, [supported]);

  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect -- the load it starts is async; state lands after the await, not in the effect
    void refresh();
  }, [refresh]);

  async function enable() {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      // Must be inside the click: every browser requires a user gesture for
      // requestPermission, and Safari drops the gesture across an await of
      // something that is not the permission call itself.
      const result = await Notification.requestPermission();
      setPerm(result as Perm);
      if (result !== "granted") {
        setError(result === "denied"
          ? "The browser blocked notifications for this site."
          : "Permission was dismissed. Tap Enable again when you are ready.");
        return;
      }
      const { public_key } = await getPushKey();
      const reg = await serviceWorkerReady();
      const sub = await reg.pushManager.subscribe({
        // Web push has no "silent" mode any browser trusts: userVisibleOnly
        // false is rejected outright by Chrome.
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(public_key) as BufferSource,
      });
      const json = sub.toJSON() as { endpoint?: string; keys?: Record<string, string> };
      const { subscriptions } = await subscribePush({
        endpoint: json.endpoint ?? sub.endpoint,
        p256dh: json.keys?.p256dh ?? keyToB64(sub.getKey("p256dh")),
        auth: json.keys?.auth ?? keyToB64(sub.getKey("auth")),
        label: navigator.userAgent.slice(0, 120),
      });
      setDevices(subscriptions);
      setSubscribedHere(true);
      setNote("This device will now get task alerts.");
    } catch (e) {
      setError(e instanceof Error ? e.message : "could not enable notifications");
    } finally {
      setBusy(false);
    }
  }

  async function disable() {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const reg = await serviceWorkerReady();
      const sub = await reg.pushManager.getSubscription();
      if (sub) {
        // Tell the server first: if unsubscribe() succeeds and the POST then
        // fails, the row survives with an endpoint nothing can deliver to and
        // every alert pays for a dead send until the service 410s it.
        const auth = sub.toJSON().keys?.auth ?? keyToB64(sub.getKey("auth"));
        await unsubscribePush(sub.endpoint, auth).catch(() => undefined);
        await sub.unsubscribe();
      }
      setSubscribedHere(false);
      await refresh();
      setNote("This device will no longer get alerts.");
    } catch (e) {
      setError(e instanceof Error ? e.message : "could not turn notifications off");
    } finally {
      setBusy(false);
    }
  }

  async function test() {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const { sent, devices: n } = await testPush();
      if (sent > 0) {
        setNote(`Sent to ${sent} of ${n} device${n === 1 ? "" : "s"}.`);
      } else {
        // An error, in the error colour. This sat in the success slot and
        // rendered green, so a send that reached nobody looked like a send
        // that worked -- which is the one thing a Test button must never do.
        // The wording no longer guesses at "expired" either: the first real
        // cause was a server-side signing failure, and a message that blames
        // the subscription sends the operator to re-subscribe a device that
        // was fine.
        setError(
          `The server could not deliver to ${n === 1 ? "the subscribed device" : `any of the ${n} subscribed devices`}. ` +
          "Nothing is wrong with this device's permission — check the agent's log for a line starting \"push:\".",
        );
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "the test failed");
    } finally {
      setBusy(false);
    }
  }

  if (!supported) {
    return (
      <p className="push-note">
        This browser has no Push API, so notifications are not available here.
        Telegram alerts above still work.
      </p>
    );
  }

  // The one case where the button would be a lie: on iOS, push exists only for
  // an app installed to the Home Screen.
  if (isIos() && !isStandalone()) {
    return (
      <p className="push-note">
        On iPhone and iPad, notifications only work once Persei is installed to the
        Home Screen. Open the Share menu and choose <strong>Add to Home Screen</strong>,
        then turn them on from inside the installed app.
      </p>
    );
  }

  return (
    <div className="push-panel">
      <div className="push-state">
        <span className={`push-dot ${subscribedHere ? "is-on" : ""}`} aria-hidden="true" />
        <span>
          {subscribedHere
            ? "On for this device"
            : perm === "denied" ? "Blocked by the browser" : "Off for this device"}
        </span>
        {devices !== null && devices > 0 && (
          <span className="push-count">
            {devices} device{devices === 1 ? "" : "s"} subscribed on this account
          </span>
        )}
      </div>

      {perm === "denied" ? (
        <p className="push-note">
          Notifications are blocked for this site, and a site cannot ask again once that
          happens. Allow them in the browser's site settings (the padlock or ⋮ menu next
          to the address bar), then reload this page.
        </p>
      ) : (
        <div className="push-actions">
          {subscribedHere ? (
            <>
              <button type="button" className="settings-btn" disabled={busy} onClick={disable}>
                Turn off for this device
              </button>
              <button type="button" className="settings-btn" disabled={busy} onClick={test}>
                {busy ? "Working…" : "Send a test"}
              </button>
            </>
          ) : (
            <button type="button" className="submit-btn" disabled={busy} onClick={enable}>
              {busy ? "Enabling…" : "Enable on this device"}
            </button>
          )}
        </div>
      )}

      {note && <p className="push-ok" role="status">{note}</p>}
      {error && <p className="push-error" role="alert">{error}</p>}

      <p className="push-note">
        You get the same alerts Telegram carries — a task finishing, escalating, or waiting
        on your approval — scoped to the projects your account can see. Notifications are
        granted per device, so each phone or laptop is turned on separately.
      </p>
    </div>
  );
}
