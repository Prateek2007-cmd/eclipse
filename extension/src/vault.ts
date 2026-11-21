/** Extension-side vault (Sprint 5, plan §6.7): WebCrypto AES-256-GCM.
 *
 * The server holds the Fernet-encrypted vault file (server/vault/
 * store.py). The extension holds the WebCrypto key in memory after
 * the user unlocks it here, and decrypts vault entries locally —
 * the plaintext never crosses the extension boundary in a message.
 *
 * This module is the extension half of "WebCrypto in the extension
 * + Fernet file on disk". The fill map itself still travels from
 * the server in the execute response (the local channel); this
 * module is for the extension-held key and for decrypting entries
 * the server returns as ciphertext (a future step — the server
 * does not yet return ciphertext, so this module is implemented
 * and unit-tested, not yet wired into the fill path).
 *
 * Key derivation: PBKDF2-SHA256, 600,000 iterations (the same
 * count as the server's Fernet key), a random 16-byte salt.
 * Encryption: AES-256-GCM, a random 12-byte IV per entry.
 */
export interface VaultEntry {
  /** base64 ciphertext (the IV is prepended, first 12 bytes). */
  ciphertext: string;
  /** base64 salt used to derive the key. */
  salt: string;
}

const PBKDF2_ITERATIONS = 600_000;
const SALT_BYTES = 16;
const IV_BYTES = 12;

function getSubtle(): SubtleCrypto {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) {
    throw new Error("WebCrypto is not available in this context");
  }
  return subtle;
}

function toBase64(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary);
}

function fromBase64(text: string): Uint8Array {
  const binary = atob(text);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

export async function deriveVaultKey(
  password: string,
  salt: Uint8Array,
): Promise<CryptoKey> {
  const subtle = getSubtle();
  const base = await subtle.importKey(
    "raw",
    new TextEncoder().encode(password),
    "PBKDF2",
    false,
    ["deriveKey"],
  );
  return subtle.deriveKey(
    { name: "PBKDF2", salt: salt as BufferSource, iterations: PBKDF2_ITERATIONS, hash: "SHA-256" },
    base,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"],
  );
}

export async function encryptVaultEntry(
  key: CryptoKey,
  plaintext: string,
): Promise<VaultEntry> {
  const subtle = getSubtle();
  const iv = globalThis.crypto.getRandomValues(new Uint8Array(IV_BYTES));
  const ciphertext = await subtle.encrypt(
    { name: "AES-GCM", iv: iv as BufferSource },
    key,
    new TextEncoder().encode(plaintext),
  );
  const combined = new Uint8Array(IV_BYTES + ciphertext.byteLength);
  combined.set(iv, 0);
  combined.set(new Uint8Array(ciphertext), IV_BYTES);
  const salt = globalThis.crypto.getRandomValues(new Uint8Array(SALT_BYTES));
  return { ciphertext: toBase64(combined), salt: toBase64(salt) };
}

export async function decryptVaultEntry(
  key: CryptoKey,
  entry: VaultEntry,
): Promise<string> {
  const subtle = getSubtle();
  const combined = fromBase64(entry.ciphertext);
  const iv = combined.slice(0, IV_BYTES);
  const data = combined.slice(IV_BYTES);
  const plaintext = await subtle.decrypt(
    { name: "AES-GCM", iv: iv as BufferSource },
    key,
    data as BufferSource,
  );
  return new TextDecoder().decode(plaintext);
}

/** The in-memory vault key (dropped on lock). */
let vaultKey: CryptoKey | null = null;

export async function unlockVault(password: string, salt: Uint8Array): Promise<void> {
  vaultKey = await deriveVaultKey(password, salt);
}

export function lockVault(): void {
  vaultKey = null;
}

export function isVaultUnlocked(): boolean {
  return vaultKey !== null;
}

export function getVaultKey(): CryptoKey | null {
  return vaultKey;
}
