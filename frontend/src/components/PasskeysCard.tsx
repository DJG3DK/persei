import { useEffect, useState } from "react";
import { deletePasskey, listPasskeys, passkeyRegisterOptions, passkeyRegisterVerify, renamePasskey, type Passkey } from "../api";
import { creationOptions, credentialJSON, passkeyErrorText, passkeysSupported } from "../passkeys";

/* Settings > Account: the passkeys on this account. Adding one asks for the
   password first (a session someone else got hold of must not plant a way
   back in); the authenticator code stays the backup sign-in. */

function when(iso: string | null): string {
  if (!iso) return "never";
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function defaultName(): string {
  const ua = navigator.userAgent;
  if (/iPhone/.test(ua)) return "iPhone";
  if (/iPad/.test(ua)) return "iPad";
  if (/Android/.test(ua)) return "Android phone";
  if (/Windows/.test(ua)) return "Windows PC";
  if (/Mac OS X/.test(ua)) return "Mac";
  if (/Linux/.test(ua)) return "Linux PC";
  return "Passkey";
}

export function PasskeysCard() {
  const [keys, setKeys] = useState<Passkey[] | null>(null);
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState(defaultName);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const supported = passkeysSupported();

  async function refresh() {
    try {
      setKeys(await listPasskeys());
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load passkeys");
    }
  }

  // The first load: a fetch from the server, guarded so a card that unmounts
  // before the answer arrives does not write into it.
  useEffect(() => {
    let live = true;
    listPasskeys()
      .then((list) => { if (live) setKeys(list); })
      .catch((err: unknown) => { if (live) setError(err instanceof Error ? err.message : "could not load passkeys"); });
    return () => { live = false; };
  }, []);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const { challenge_id, options } = await passkeyRegisterOptions(password);
      const cred = await navigator.credentials.create({ publicKey: creationOptions(options) });
      if (!cred) throw new Error("no passkey was created");
      const saved = await passkeyRegisterVerify(challenge_id, credentialJSON(cred as PublicKeyCredential), name);
      setNotice(`"${saved.name}" added. You can sign in with it now.`);
      setAdding(false);
      setPassword("");
      await refresh();
    } catch (err) {
      setError(passkeyErrorText(err, "could not add the passkey"));
    } finally {
      setBusy(false);
    }
  }

  async function rename(key: Passkey) {
    const next = window.prompt("Name this passkey", key.name);
    if (next === null || !next.trim() || next.trim() === key.name) return;
    // Each action starts clean: until 2026-10-22 (audit U28) a failed add's
    // error stayed on screen through a successful rename or remove.
    setError(null);
    setNotice(null);
    try {
      await renamePasskey(key.id, next);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not rename");
    }
  }

  async function remove(key: Passkey) {
    if (!window.confirm(`Remove "${key.name}"? It will no longer sign you in.`)) return;
    setError(null);
    setNotice(null);
    try {
      await deletePasskey(key.id);
      setNotice(`"${key.name}" removed.`);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not remove");
    }
  }

  return (
    <section className="settings-card passkeys-card">
      <h2>
        Passkeys{" "}
        <span className={`settings-pill ${keys && keys.length ? "settings-pill--on" : ""}`}>
          {keys ? keys.length : "…"}
        </span>
      </h2>
      <p className="settings-card-sub">
        Sign in with your phone, laptop or security key instead of your password and code. Your device asks for
        its fingerprint, face or PIN, so a passkey is both factors at once, and it only works on this site.
        Your password and authenticator code keep working as the backup.
      </p>
      {keys && keys.length > 0 && (
        <ul className="passkeys-list">
          {keys.map((k) => (
            <li key={k.id}>
              <div className="passkeys-name">
                <strong>{k.name}</strong>
                {k.synced && <span className="settings-pill settings-pill--on">synced</span>}
              </div>
              <div className="passkeys-meta">
                added {when(k.created_at)} · last used {when(k.last_used_at)}
              </div>
              <div className="passkeys-actions">
                <button type="button" className="passkeys-link" onClick={() => rename(k)}>Rename</button>
                <button type="button" className="passkeys-link passkeys-link--danger" onClick={() => remove(k)}>Remove</button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {notice && <div className="settings-notice" role="status">{notice}</div>}
      {error && <div className="settings-error" role="alert">{error}</div>}
      {!supported ? (
        <p className="settings-card-sub">This browser can't create passkeys.</p>
      ) : adding ? (
        <form onSubmit={add} className="settings-form">
          <label className="field">
            <span>Name</span>
            <input value={name} maxLength={60} onChange={(e) => setName(e.target.value)} required />
          </label>
          <label className="field">
            <span>Current password</span>
            <input type="password" autoComplete="current-password" value={password}
                   onChange={(e) => setPassword(e.target.value)} required />
          </label>
          <div className="passkeys-form-actions">
            <button className="submit-btn" type="submit" disabled={busy || !password || !name.trim()}>
              {busy ? "Waiting for your device…" : "Create passkey"}
            </button>
            <button type="button" className="passkeys-link" onClick={() => { setAdding(false); setError(null); }}>
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <button className="submit-btn" type="button" onClick={() => { setAdding(true); setNotice(null); }}>
          Add a passkey
        </button>
      )}
    </section>
  );
}
