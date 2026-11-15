/** Minimal reader for legacy Solana transactions. Only what the wallet needs to check what it signed. */

export interface LegacyTx {
  signatures: Uint8Array[];
  message: Uint8Array;
  numRequiredSignatures: number;
  accountKeys: Uint8Array[];
}

function readShortVec(b: Uint8Array, start: number): [number, number] {
  let value = 0;
  let shift = 0;
  let i = start;
  for (;;) {
    if (i >= b.length || shift > 14) throw new Error("bad length prefix");
    const byte = b[i++];
    value |= (byte & 0x7f) << shift;
    if ((byte & 0x80) === 0) return [value, i];
    shift += 7;
  }
}

export function bytesEqual(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

/** Throws for anything that is not a well formed legacy (non versioned) transaction. */
export function parseLegacyTx(wire: Uint8Array): LegacyTx {
  const [nSigs, afterCount] = readShortVec(wire, 0);
  let off = afterCount;
  if (off + nSigs * 64 > wire.length) throw new Error("truncated transaction");
  const signatures: Uint8Array[] = [];
  for (let i = 0; i < nSigs; i++, off += 64) signatures.push(wire.slice(off, off + 64));
  const message = wire.slice(off);
  if (message.length < 4) throw new Error("truncated message");
  if (message[0] & 0x80) throw new Error("versioned transactions are not supported");
  const numRequiredSignatures = message[0];
  const [nKeys, keysAt] = readShortVec(message, 3);
  if (keysAt + nKeys * 32 > message.length) throw new Error("truncated account keys");
  const accountKeys: Uint8Array[] = [];
  for (let i = 0; i < nKeys; i++) accountKeys.push(message.slice(keysAt + i * 32, keysAt + (i + 1) * 32));
  if (nSigs !== numRequiredSignatures) throw new Error("signature slots do not match the message");
  return { signatures, message, numRequiredSignatures, accountKeys };
}
