/** Extension-side vault tests (Sprint 5): WebCrypto AES-256-GCM.
 *
 * Run: npm test (pretest compiles src/vault.ts to tests-dist/).
 * Uses Node's webcrypto (globalThis.crypto).
 */
import test from "node:test";
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";

import {
  deriveVaultKey,
  encryptVaultEntry,
  decryptVaultEntry,
  unlockVault,
  lockVault,
  isVaultUnlocked,
} from "../tests-dist/vault.js";

// Node's webcrypto is not on globalThis by default in a test
// file — install it so the vault module's getSubtle() finds it.
if (!globalThis.crypto) {
  globalThis.crypto = webcrypto;
}

test("vault: encrypt then decrypt round-trips the value", async () => {
  const salt = webcrypto.getRandomValues(new Uint8Array(16));
  const key = await deriveVaultKey("correct horse battery staple", salt);
  const entry = await encryptVaultEntry(key, "4111 1111 1111 1111");
  assert.notEqual(entry.ciphertext, "4111 1111 1111 1111");
  assert.equal(await decryptVaultEntry(key, entry), "4111 1111 1111 1111");
});

test("vault: a wrong key cannot decrypt the entry", async () => {
  const salt = webcrypto.getRandomValues(new Uint8Array(16));
  const key = await deriveVaultKey("correct horse battery staple", salt);
  const entry = await encryptVaultEntry(key, "s3cr3t");
  const wrong = await deriveVaultKey("wrong password", salt);
  await assert.rejects(() => decryptVaultEntry(wrong, entry));
});

test("vault: lock drops the key from memory", async () => {
  const salt = webcrypto.getRandomValues(new Uint8Array(16));
  await unlockVault("correct horse battery staple", salt);
  assert.equal(isVaultUnlocked(), true);
  lockVault();
  assert.equal(isVaultUnlocked(), false);
});
