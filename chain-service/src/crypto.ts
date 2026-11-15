// ed25519 over node:crypto (no extra dependency): keys are wrapped in the fixed DER prefixes.
import { createPrivateKey, createPublicKey, sign, verify } from "node:crypto";

const SPKI_PREFIX = Buffer.from("302a300506032b6570032100", "hex");
const PKCS8_PREFIX = Buffer.from("302e020100300506032b657004220420", "hex");

export function verifyEd25519(publicKey: Uint8Array, message: Uint8Array, signature: Uint8Array): boolean {
  if (publicKey.length !== 32 || signature.length !== 64) return false;
  try {
    const key = createPublicKey({ key: Buffer.concat([SPKI_PREFIX, publicKey]), format: "der", type: "spki" });
    return verify(null, message, key, signature);
  } catch {
    return false;
  }
}

/** secretKey is the 64 byte Solana keypair (32 byte seed, then the public key) */
export function signEd25519(secretKey: Uint8Array, message: Uint8Array): Buffer {
  const key = createPrivateKey({ key: Buffer.concat([PKCS8_PREFIX, secretKey.subarray(0, 32)]), format: "der", type: "pkcs8" });
  return sign(null, message, key);
}
