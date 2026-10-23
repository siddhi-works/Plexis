// A real MCP client (TypeScript SDK) standing in for the Alexa+ host.
// It negotiates protocol 2025-11-25 over Streamable HTTP, authenticates with the
// Alexa+ two-tier OAuth model, answers elicitation requests, hosts MCP Apps, and
// records every JSON-RPC message for the protocol trace.
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import {
  CallToolResultSchema,
  ElicitRequestSchema,
  LoggingMessageNotificationSchema,
  ResourceUpdatedNotificationSchema,
  type CallToolResult,
  type JSONRPCMessage,
} from "@modelcontextprotocol/sdk/types.js";

export const SERVER: string = (import.meta.env.VITE_QED_URL as string | undefined) ?? "http://127.0.0.1:8000";
const CLIENT_ID = "alexa-plus-simulator";
const CLIENT_SECRET = "dev-secret-change-me";

export type TraceEvent = {
  seq: number;
  at: number;
  dir: "out" | "in";
  kind: "request" | "response" | "notification" | "error";
  method?: string;
  id?: string | number;
  ms?: number;
  size: number;
  body: unknown;
};

export type ElicitAnswer = { action: "accept" | "decline" | "cancel"; content?: Record<string, unknown> };
export type ElicitAsk = { message: string; schema: { properties?: Record<string, any>; required?: string[] } };

export type Hooks = {
  onTrace: (e: TraceEvent) => void;
  onElicit: (ask: ElicitAsk) => Promise<ElicitAnswer>;
  onNotify: (kind: string, data: any) => void;
};

let traceSeq = 0;

const b64url = (bytes: ArrayBuffer | Uint8Array) =>
  btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

/** Tier 1 (service) or tier 2 (account linked, PKCE) access token. */
export async function getToken(linked: boolean): Promise<{ token: string; scope: string }> {
  if (!linked) {
    const r = await fetch(`${SERVER}/oauth/token`, {
      method: "POST",
      headers: { Authorization: "Basic " + btoa(`${CLIENT_ID}:${CLIENT_SECRET}`), "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ grant_type: "client_credentials", scope: "mcp:service" }),
    });
    if (!r.ok) throw new Error(`token endpoint returned ${r.status}`);
    const j = await r.json();
    return { token: j.access_token, scope: j.scope };
  }
  const verifier = b64url(crypto.getRandomValues(new Uint8Array(48)));
  const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  const auth = await fetch(
    `${SERVER}/oauth/authorize?` +
      new URLSearchParams({ response_type: "code", client_id: CLIENT_ID, code_challenge: challenge, code_challenge_method: "S256", format: "json", scope: "mcp:tools mcp:resources" }),
  );
  if (!auth.ok) throw new Error(`authorize returned ${auth.status}`);
  const { code } = await auth.json();
  const r = await fetch(`${SERVER}/oauth/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ grant_type: "authorization_code", code, code_verifier: verifier, client_id: CLIENT_ID }),
  });
  if (!r.ok) throw new Error(`token exchange returned ${r.status}`);
  const j = await r.json();
  return { token: j.access_token, scope: j.scope };
}

export class QedClient {
  client: Client;
  transport!: StreamableHTTPClientTransport;
  protocolVersion = "";
  serverName = "";
  tools: { name: string; title?: string; meta?: any }[] = [];
  private started = new Map<string | number, number>();

  constructor(private hooks: Hooks) {
    this.client = new Client(
      { name: "alexa-plus-simulator", title: "Alexa+ host (simulated)", version: "1.0.0" },
      { capabilities: { elicitation: { form: {} } } },
    );
  }

  private record(dir: "in" | "out", msg: JSONRPCMessage) {
    const m = msg as any;
    let kind: TraceEvent["kind"] = "notification";
    let ms: number | undefined;
    if (m.method && m.id !== undefined) {
      kind = "request";
      if (dir === "out") this.started.set(m.id, performance.now());
    } else if (m.id !== undefined) {
      kind = m.error ? "error" : "response";
      const s = this.started.get(m.id);
      if (s !== undefined) ms = Math.round(performance.now() - s);
    }
    const text = JSON.stringify(msg);
    this.hooks.onTrace({ seq: ++traceSeq, at: Date.now(), dir, kind, method: m.method, id: m.id, ms, size: text.length, body: msg });
  }

  async connect(token: string) {
    const transport = new StreamableHTTPClientTransport(new URL(`${SERVER}/mcp`), {
      requestInit: { headers: { Authorization: `Bearer ${token}` } },
    });
    const send = transport.send.bind(transport);
    transport.send = async (message: JSONRPCMessage | JSONRPCMessage[], options?: any) => {
      (Array.isArray(message) ? message : [message]).forEach((m) => this.record("out", m));
      return send(message as any, options);
    };
    // The SDK installs its message handler and then calls start(); wrapping at that
    // point captures every inbound message, including the initialize response.
    const start = transport.start.bind(transport);
    transport.start = async () => {
      const handler = transport.onmessage;
      transport.onmessage = (m: JSONRPCMessage) => {
        this.record("in", m);
        handler?.(m);
      };
      return start();
    };
    this.transport = transport;

    this.client.setRequestHandler(ElicitRequestSchema, async (req) => {
      const p: any = req.params;
      const answer = await this.hooks.onElicit({ message: p.message, schema: p.requestedSchema ?? {} });
      return answer as any;
    });
    this.client.setNotificationHandler(LoggingMessageNotificationSchema, async (n) => {
      this.hooks.onNotify("log", n.params.data);
    });
    this.client.setNotificationHandler(ResourceUpdatedNotificationSchema, async (n) => {
      this.hooks.onNotify("resource", n.params.uri);
    });

    await this.client.connect(transport);
    this.protocolVersion = transport.protocolVersion ?? "2025-11-25";
    this.serverName = this.client.getServerVersion()?.name ?? "";
    const listed = await this.client.listTools();
    this.tools = listed.tools.map((t) => ({ name: t.name, title: t.title, meta: (t as any)._meta }));
  }

  async call(name: string, args: Record<string, unknown>, onProgress?: (p: { progress: number; total?: number; message?: string }) => void) {
    const t0 = performance.now();
    const result = (await this.client.callTool({ name, arguments: args }, CallToolResultSchema, {
      onprogress: onProgress as any,
      timeout: 60_000,
      resetTimeoutOnProgress: true,
    })) as CallToolResult;
    return { result, ms: Math.round(performance.now() - t0) };
  }

  async readText(uri: string): Promise<string> {
    const r = await this.client.readResource({ uri });
    const c: any = r.contents[0];
    return c?.text ?? "";
  }

  async close() {
    try {
      await this.transport?.terminateSession();
    } catch {
      /* server may already be gone */
    }
    await this.client.close();
  }
}
