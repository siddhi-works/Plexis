// Client-side receipt verification: the browser does not trust the server's
// "ok". It recomputes each ledger entry hash with WebCrypto, checks the chain
// links, and verifies every Ed25519 signature with the published public key.
import * as ed from "@noble/ed25519";
import { SERVER } from "./mcp";

export type LedgerEntry = {
  index: number;
  kind: string;
  certHash: string;
  prevHash: string;
  entryHash: string;
  signature: string;
  keyId: string;
  createdAt: string;
};

export type EntryCheck = { entry: LedgerEntry; hashOk: boolean; linkOk: boolean; sigOk: boolean };

const hex = (buf: ArrayBuffer) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
const fromB64 = (s: string) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

export async function sha256Hex(text: string): Promise<string> {
  return hex(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)));
}

export async function fetchLedger(): Promise<{ publicKey: string; keyId: string; entries: LedgerEntry[] }> {
  const r = await fetch(`${SERVER}/dev/ledger`);
  if (!r.ok) throw new Error(`ledger endpoint returned ${r.status}`);
  return r.json();
}

export async function verifyEntries(entries: LedgerEntry[], publicKeyB64: string, tamperIndex?: number): Promise<EntryCheck[]> {
  const pub = fromB64(publicKeyB64);
  const out: EntryCheck[] = [];
  for (let i = 0; i < entries.length; i++) {
    const e = { ...entries[i] };
    if (e.index === tamperIndex) e.certHash = e.certHash.replace(/^./, (c) => (c === "a" ? "b" : "a"));
    const expected = await sha256Hex(`${e.index}|${e.kind}|${e.certHash}|${e.prevHash}|${e.createdAt}`);
    const hashOk = expected === e.entryHash;
    const prev = i > 0 ? entries[i - 1] : undefined;
    const linkOk = prev ? prev.entryHash === e.prevHash : true;
    let sigOk = false;
    try {
      sigOk = await ed.verifyAsync(fromB64(e.signature), new TextEncoder().encode(hashOk ? e.entryHash : expected), pub);
    } catch {
      sigOk = false;
    }
    out.push({ entry: entries[i], hashOk, linkOk, sigOk });
  }
  return out;
}
