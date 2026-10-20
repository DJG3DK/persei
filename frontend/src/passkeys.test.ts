import { describe, expect, it } from "vitest";
import { creationOptions, fromBase64url, passkeyErrorText, requestOptions, toBase64url } from "./passkeys";

describe("passkeys helpers", () => {
  it("round-trips base64url, padding and the url-safe alphabet", () => {
    const bytes = new Uint8Array([0, 255, 62, 63, 250, 1, 2]);
    const s = toBase64url(bytes);
    expect(s).not.toMatch(/[+/=]/);
    expect(new Uint8Array(fromBase64url(s))).toEqual(bytes);
  });

  it("turns the server's JSON into the binary options the browser wants", () => {
    const opts = creationOptions({
      challenge: toBase64url(new Uint8Array([1, 2, 3])),
      rp: { id: "agent.example.com", name: "Persei" },
      user: { id: toBase64url(new Uint8Array([9, 9])), name: "d@x", displayName: "d@x" },
      pubKeyCredParams: [{ type: "public-key", alg: -7 }],
      excludeCredentials: [{ type: "public-key", id: toBase64url(new Uint8Array([7])) }],
    });
    expect(new Uint8Array(opts.challenge as ArrayBuffer)).toEqual(new Uint8Array([1, 2, 3]));
    expect(new Uint8Array(opts.user.id as ArrayBuffer)).toEqual(new Uint8Array([9, 9]));
    expect(new Uint8Array(opts.excludeCredentials![0].id as ArrayBuffer)).toEqual(new Uint8Array([7]));
    const req = requestOptions({ challenge: toBase64url(new Uint8Array([4])), rpId: "agent.example.com" });
    expect(new Uint8Array(req.challenge as ArrayBuffer)).toEqual(new Uint8Array([4]));
  });

  it("says what a cancelled prompt means instead of showing an error code", () => {
    expect(passkeyErrorText(new DOMException("x", "NotAllowedError"), "f")).toMatch(/cancelled or timed out/);
    expect(passkeyErrorText(new DOMException("x", "InvalidStateError"), "f")).toMatch(/already has a passkey/);
    expect(passkeyErrorText(new Error("server said no"), "f")).toBe("server said no");
  });
});
