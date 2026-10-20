import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PasskeysCard } from "./PasskeysCard";

const listPasskeys = vi.fn();
const passkeyRegisterOptions = vi.fn();
const passkeyRegisterVerify = vi.fn();
const deletePasskey = vi.fn();
const renamePasskey = vi.fn();

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  listPasskeys: (...a: unknown[]) => listPasskeys(...a),
  passkeyRegisterOptions: (...a: unknown[]) => passkeyRegisterOptions(...a),
  passkeyRegisterVerify: (...a: unknown[]) => passkeyRegisterVerify(...a),
  deletePasskey: (...a: unknown[]) => deletePasskey(...a),
  renamePasskey: (...a: unknown[]) => renamePasskey(...a),
}));

const PHONE = { id: 7, name: "Danny's phone", site: "agent.example.com", synced: true,
  created_at: "2026-10-01T10:00:00Z", last_used_at: null };

beforeEach(() => {
  [listPasskeys, passkeyRegisterOptions, passkeyRegisterVerify, deletePasskey, renamePasskey].forEach((f) => f.mockReset());
  vi.stubGlobal("PublicKeyCredential", function PublicKeyCredential() {});
});

describe("PasskeysCard", () => {
  it("lists the account's passkeys", async () => {
    listPasskeys.mockResolvedValue([PHONE]);
    render(<PasskeysCard />);
    expect(await screen.findByText("Danny's phone")).toBeInTheDocument();
    expect(screen.getByText(/last used never/i)).toBeInTheDocument();
    expect(screen.getByText("synced")).toBeInTheDocument();
  });

  it("adds a passkey only after the password, then lists it", async () => {
    listPasskeys.mockResolvedValueOnce([]).mockResolvedValueOnce([PHONE]);
    const create = vi.fn().mockResolvedValue({ toJSON: () => ({ id: "n", rawId: "n", type: "public-key", response: {} }) });
    Object.defineProperty(navigator, "credentials", { configurable: true, value: { create, get: vi.fn() } });
    passkeyRegisterOptions.mockResolvedValue({ challenge_id: "r1", options: {
      challenge: "AQID", rp: { id: "agent.example.com", name: "Persei" },
      user: { id: "CQk", name: "d", displayName: "d" }, pubKeyCredParams: [] } });
    passkeyRegisterVerify.mockResolvedValue(PHONE);
    render(<PasskeysCard />);
    await userEvent.click(await screen.findByRole("button", { name: /add a passkey/i }));
    const create_btn = screen.getByRole("button", { name: /create passkey/i });
    expect(create_btn).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/current password/i), "Correct-Horse-9");
    await userEvent.click(create_btn);
    expect(passkeyRegisterOptions).toHaveBeenCalledWith("Correct-Horse-9");
    expect(create).toHaveBeenCalled();
    expect(passkeyRegisterVerify).toHaveBeenCalledWith("r1", expect.objectContaining({ id: "n" }), expect.any(String));
    expect(await screen.findByText(/added\. you can sign in with it now/i)).toBeInTheDocument();
    expect(await screen.findByText("Danny's phone")).toBeInTheDocument();
  });

  it("removes one after a confirm", async () => {
    listPasskeys.mockResolvedValueOnce([PHONE]).mockResolvedValueOnce([]);
    deletePasskey.mockResolvedValue({ ok: true });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<PasskeysCard />);
    await userEvent.click(await screen.findByRole("button", { name: /remove/i }));
    expect(deletePasskey).toHaveBeenCalledWith(7);
    expect(await screen.findByText(/"Danny's phone" removed/)).toBeInTheDocument();
  });
});
