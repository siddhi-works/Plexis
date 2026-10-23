// MCP Apps host: renders the add-on's ui:// resource in a sandboxed iframe and
// bridges JSON-RPC over postMessage (ui/initialize, tool-result, tools/call).
import { useEffect, useRef, useState } from "react";

type Props = {
  html: string;
  result: unknown;
  displayMode: "inline" | "fullscreen" | "voice-only";
  onToolCall: (name: string, args: Record<string, unknown>) => Promise<unknown>;
  onAppEvent: (name: string, result: any) => void;
};

export function CardFrame({ html, result, displayMode, onToolCall, onAppEvent }: Props) {
  const ref = useRef<HTMLIFrameElement>(null);
  const ready = useRef(false);
  const latest = useRef(result);
  const [height, setHeight] = useState(480);
  latest.current = result;

  const post = (msg: unknown) => ref.current?.contentWindow?.postMessage(msg, "*");

  useEffect(() => {
    const onMessage = async (ev: MessageEvent) => {
      if (ev.source !== ref.current?.contentWindow) return;
      const m = ev.data;
      if (!m || m.jsonrpc !== "2.0") return;
      if (m.method === "ui/initialize") {
        post({ jsonrpc: "2.0", id: m.id, result: {
          protocolVersion: "2025-11-21",
          hostInfo: { name: "alexa-plus-simulator", version: "1.0.0" },
          hostCapabilities: { serverTools: {}, openLinks: {} },
          hostContext: { displayMode, theme: "dark", platform: "echo-show" },
        } });
      } else if (m.method === "ui/notifications/initialized") {
        ready.current = true;
        if (latest.current) post({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: latest.current });
      } else if (m.method === "ui/notifications/size-changed") {
        setHeight(Math.max(240, Math.ceil(m.params?.height ?? 480)));
      } else if (m.method === "tools/call") {
        try {
          const res = await onToolCall(m.params.name, m.params.arguments ?? {});
          post({ jsonrpc: "2.0", id: m.id, result: res });
        } catch (err) {
          post({ jsonrpc: "2.0", id: m.id, error: { code: -32000, message: String(err) } });
        }
      } else if (m.method === "ui/notifications/tool-called") {
        onAppEvent(m.params?.name, m.params?.result);
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [displayMode, onToolCall, onAppEvent]);

  useEffect(() => {
    if (ready.current && result) post({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: result });
  }, [result]);

  useEffect(() => {
    if (ready.current) post({ jsonrpc: "2.0", method: "ui/notifications/host-context-changed", params: { displayMode, theme: "dark" } });
  }, [displayMode]);

  return (
    <iframe
      ref={ref}
      className="card-frame"
      title="Add-on screen"
      sandbox="allow-scripts"
      srcDoc={html}
      style={{ height }}
    />
  );
}
