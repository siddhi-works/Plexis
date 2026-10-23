import { useCallback, useEffect, useRef, useState } from "react";
import { CardFrame } from "./components/CardFrame";
import { Console, type CallRecord } from "./components/Console";
import { ElicitSheet } from "./components/ElicitSheet";
import { route, type HostContext } from "./host";
import { getToken, QedClient, type ElicitAnswer, type ElicitAsk, type TraceEvent } from "./mcp";
import { isSpeaking, listen, matchSpoken, speak, speechSupported, stopSpeaking, type ListenEnd } from "./voice";

type Turn = { who: "you" | "assistant" | "notice"; text: string; tool?: string };
type Mode = "show" | "voice";

const SCENARIOS: { label: string; text: string }[] = [
  { label: "Start a conversation", text: "Book a trip for me" },
  { label: "Family trip, all at once", text: "Plan a 3 night trip to San Francisco on November 13 with Grandma and Maya under $3,000, private room with AC, at least one Thai dinner and a museum" },
  { label: "Impossible ask", text: "Plan 3 nights from Seattle to San Francisco on November 13 for 2 people under $1,400, wheelchair accessible hotel rated 4.7 or better, no AC" },
  { label: "Why not…", text: "Why not the Copper Hotel?" },
  { label: "Better weather?", text: "Are there better dates for the weather?" },
  { label: "What to pack", text: "What should we pack and wear?" },
  { label: "Call the hotel", text: "Call the hotel and ask if we can check in early and if there's a roll-in shower" },
  { label: "Verify receipt", text: "Verify my receipt" },
];
// Statuses where the add-on is waiting for the customer to say something next.
const EXPECTS_REPLY = new Set(["needs_input", "infeasible", "planned", "confirmation_required", "weather", "disrupted"]);
const MIC_NOTES: Partial<Record<ListenEnd, string>> = {
  "no-speech": "I didn't catch that. Tap the ring to talk.",
  "not-allowed": "The microphone is blocked. Allow it in the browser's address bar, then tap the ring.",
  unsupported: "Voice input needs Chrome or Edge. You can type instead.",
  error: "The microphone stopped unexpectedly. Tap the ring to try again.",
};

export default function App() {
  const [linked, setLinked] = useState(true);
  const [mode, setMode] = useState<Mode>("show");
  const [voiceOn, setVoiceOn] = useState(true);
  const [handsFree, setHandsFree] = useState(true);
  const [status, setStatus] = useState<"connecting" | "ready" | "error">("connecting");
  const [statusText, setStatusText] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [trace, setTrace] = useState<TraceEvent[]>([]);
  const [calls, setCalls] = useState<CallRecord[]>([]);
  const [card, setCard] = useState<{ html: string; result: any } | null>(null);
  const [lastData, setLastData] = useState<any>(null);
  const [ask, setAsk] = useState<(ElicitAsk & { resolve: (a: ElicitAnswer) => void }) | null>(null);
  const [busy, setBusy] = useState("");
  const [listening, setListening] = useState(false);
  const [draft, setDraft] = useState("");
  const [micNote, setMicNote] = useState("");
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [alert, setAlert] = useState<string>("");
  const [ledgerTick, setLedgerTick] = useState(0);
  const [bookedOutbound, setBookedOutbound] = useState<string | undefined>();
  const ctx = useRef<HostContext>({});
  const client = useRef<QedClient | null>(null);
  const cardHtml = useRef<string>("");
  const stopListen = useRef<() => void>(() => {});
  const askRef = useRef(ask);
  askRef.current = ask;
  const prefs = useRef({ voiceOn, handsFree });
  prefs.current = { voiceOn, handsFree };
  const lastDataRef = useRef<any>(null);
  lastDataRef.current = lastData;
  const submitRef = useRef<(text: string) => Promise<void>>(async () => {});

  // ---------------------------------------------------------------- listening

  const startListening = useCallback(() => {
    if (!speechSupported()) { setMicNote(MIC_NOTES.unsupported!); return; }
    stopSpeaking();
    stopListen.current();
    setMicNote("");
    setListening(true);
    stopListen.current = listen(
      (text, final) => {
        setDraft(text);
        if (!final) return;
        setListening(false);
        const open = askRef.current;
        if (open) {
          answerByVoice(open, text);
          setDraft("");
        } else {
          submitRef.current(text);
        }
      },
      (why) => {
        setListening(false);
        if (why !== "done" && why !== "aborted") setMicNote(MIC_NOTES[why] ?? "");
      },
    );
  }, []);

  const answerByVoice = (open: ElicitAsk & { resolve: (a: ElicitAnswer) => void }, text: string) => {
    const props = open.schema.properties ?? {};
    const keys = Object.keys(props);
    const key = keys[0];
    const isBool = keys.length === 1 && props[key]?.type === "boolean";
    const options = keys.length === 1 && Array.isArray(props[key]?.enum) ? (props[key].enum as string[]) : null;
    const m = matchSpoken(text, options, isBool);
    if (m.decline) { open.resolve({ action: "decline" }); setAsk(null); return; }
    if (m.value !== undefined && key) { open.resolve({ action: "accept", content: { [key]: m.value } }); setAsk(null); return; }
    setMicNote(isBool ? "Say yes or no, or tap a button." : "Say one of the options, or tap it.");
    if (prefs.current.handsFree) setTimeout(startListening, 400);
  };

  const sayThenListen = useCallback(async (text: string, expectsReply: boolean) => {
    await speak(text, prefs.current.voiceOn);
    if (expectsReply && prefs.current.handsFree && !isSpeaking()) startListening();
  }, [startListening]);

  // ---------------------------------------------------------------- connection

  useEffect(() => {
    let cancelled = false;
    setStatus("connecting");
    (async () => {
      try {
        await client.current?.close();
        const { token, scope } = await getToken(linked);
        const c = new QedClient({
          onTrace: (e) => setTrace((t) => [...t.slice(-400), e]),
          onElicit: (a) => new Promise<ElicitAnswer>((resolve) => {
            setAsk({ ...a, resolve });
            sayThenListen(a.message, true);
            // Never leave the add-on waiting forever on an unanswered question.
            setTimeout(() => { if (askRef.current?.message === a.message) { resolve({ action: "cancel" }); setAsk(null); } }, 55_000);
          }),
          onNotify: (kind, data) => {
            if (kind === "log" && data?.type === "qed/disruption") {
              setAlert(data.message);
              setTurns((t) => [...t, { who: "notice", text: data.message }]);
              ctx.current.lastStatus = "disrupted";
              setSuggestions(["Yes, rebook me", "Show my trip status"]);
              sayThenListen(data.message, true);
            }
          },
        });
        await c.connect(token);
        cardHtml.current = await c.readText("ui://qed/trip-card.html");
        if (cancelled) return c.close();
        client.current = c;
        setStatus("ready");
        setStatusText(`Add-on "${c.serverName}" over MCP ${c.protocolVersion}, ${c.tools.length} tools, token scope: ${scope}`);
      } catch (e) {
        if (!cancelled) { setStatus("error"); setStatusText(`Can't reach the add-on at its MCP endpoint. Start it with "qed dev". (${e})`); }
      }
    })();
    return () => { cancelled = true; };
  }, [linked, sayThenListen]);

  // ---------------------------------------------------------------- tool calls

  const callTool = useCallback(async (tool: string, args: Record<string, unknown>) => {
    const c = client.current;
    if (!c) throw new Error("not connected");
    const { result, ms } = await c.call(tool, args, (p) => setBusy(p.message ?? "Working…"));
    const sc: any = result.structuredContent ?? {};
    setCalls((cs) => [...cs.slice(-60), { tool, ms, serverMs: sc.trace?.totalMs, stages: sc.trace?.stages ?? [], graph: sc.trace?.graph ?? [], status: sc.status, at: Date.now() }]);
    if (sc.planId) ctx.current.planId = sc.planId;
    if (sc.draftId) ctx.current.draftId = sc.draftId;
    if (sc.tripId) ctx.current.tripId = sc.tripId;
    if (sc.status) ctx.current.lastStatus = sc.status;
    if (sc.data?.receipt?.index !== undefined) ctx.current.receiptIndex = sc.data.receipt.index;
    if (sc.status === "booked") {
      const first = lastDataRef.current?.itinerary?.[0]?.items?.find((i: any) => ["flight", "train", "drive"].includes(i.kind));
      setBookedOutbound(first?.id);
    }
    setSuggestions(Array.isArray(sc.voiceOptions) ? sc.voiceOptions : []);
    const hasCard = (result as any)._meta?.ui?.resourceUri;
    if (hasCard && sc.data && (sc.data.trip || sc.data.conflict)) {
      setCard({ html: cardHtml.current, result });
      setLastData(sc.data);
    } else if (sc.status === "needs_input") {
      setCard(null); // a new conversation: show the question, not an old plan
    }
    setLedgerTick((n) => n + 1);
    return result;
  }, []);

  const submit = useCallback(async (text: string) => {
    const t = text.trim();
    if (!t || !client.current) return;
    if (busy) { setMicNote("Still working on the last request…"); return; }
    stopSpeaking();
    setDraft("");
    setAlert("");
    setMicNote("");
    setTurns((ts) => [...ts, { who: "you", text: t }]);
    const r = route(t, ctx.current);
    setBusy("Thinking…");
    try {
      const result: any = await callTool(r.tool, r.args);
      const sc = result.structuredContent ?? {};
      const speech = sc.speech ?? result.content?.[0]?.text ?? "";
      setTurns((ts) => [...ts, { who: "assistant", text: speech, tool: `${r.tool} (${r.why})` }]);
      setBusy("");
      sayThenListen(speech, EXPECTS_REPLY.has(sc.status));
    } catch (e) {
      setBusy("");
      const msg = `Something went wrong talking to the add-on: ${e}`;
      setTurns((ts) => [...ts, { who: "assistant", text: msg }]);
    }
  }, [busy, callTool, sayThenListen]);
  submitRef.current = submit;

  const onAppToolCall = useCallback(async (name: string, args: Record<string, unknown>) => {
    setBusy("Working…");
    try {
      const result: any = await callTool(name, args);
      const sc = result.structuredContent ?? {};
      setTurns((ts) => [...ts, { who: "assistant", text: sc.speech ?? "", tool: `${name} (tapped on screen)` }]);
      setBusy("");
      sayThenListen(sc.speech ?? "", EXPECTS_REPLY.has(sc.status));
      return result;
    } finally {
      setBusy("");
    }
  }, [callTool, sayThenListen]);

  const onAppEvent = useCallback(() => {}, []);

  const ringPressed = () => {
    if (listening) { stopListen.current(); setListening(false); return; }
    startListening(); // also interrupts the assistant if it is still talking
  };

  const answer = (a: ElicitAnswer) => {
    stopListen.current();
    setListening(false);
    ask?.resolve(a);
    setAsk(null);
  };

  const lastAssistant = [...turns].reverse().find((t) => t.who !== "you");
  const lastYou = [...turns].reverse().find((t) => t.who === "you");
  const ringState = listening ? "listening" : busy ? "thinking" : alert ? "alert" : "idle";
  const showCard = mode === "show" && card;

  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <span className="wordmark">Plexis</span>
          <span className="tag">Trips you can trust, as an Alexa+ add-on</span>
        </div>
        <div className="controls">
          <span className={`conn ${status}`} title={statusText}>{status === "ready" ? "Connected" : status === "connecting" ? "Connecting…" : "Not connected"}</span>
          <label className="switch"><input type="checkbox" checked={linked} onChange={(e) => setLinked(e.target.checked)} /> Account linked</label>
          <label className="switch"><input type="checkbox" checked={voiceOn} onChange={(e) => { setVoiceOn(e.target.checked); if (!e.target.checked) stopSpeaking(); }} /> Spoken replies</label>
          <label className="switch"><input type="checkbox" checked={handsFree} onChange={(e) => setHandsFree(e.target.checked)} /> Hands-free</label>
          <div className="seg" role="radiogroup" aria-label="Device">
            <button role="radio" aria-checked={mode === "show"} className={mode === "show" ? "on" : ""} onClick={() => setMode("show")}>Screen</button>
            <button role="radio" aria-checked={mode === "voice"} className={mode === "voice" ? "on" : ""} onClick={() => setMode("voice")}>Voice only</button>
          </div>
        </div>
      </header>

      <main className="stage">
        <section className="device-col" aria-label="Simulated Alexa+ device">
          <div className={`device ${mode}`}>
            <div className="screen">
              {alert && <div className="alert" role="alert"><strong>Trip alert.</strong> {alert}<div className="alert-actions"><button className="btn btn-go" onClick={() => submit("Yes, rebook me")}>Rebook me</button><button className="btn btn-quiet" onClick={() => setAlert("")}>Later</button></div></div>}
              {showCard ? (
                <div className="screen-scroll"><CardFrame html={card.html} result={card.result} displayMode="fullscreen" onToolCall={onAppToolCall} onAppEvent={onAppEvent} /></div>
              ) : (
                <div className="speech-screen">
                  <p className="heard">{listening ? (draft || "Listening…") : lastYou?.text ?? "Say “book a trip for me”, or tap the ring and talk."}</p>
                  <p className="said">{lastAssistant?.text ?? ""}</p>
                  {mode === "show" && suggestions.length > 0 && !busy && (
                    <div className="screen-choices">
                      {suggestions.map((s, i) => <button key={s} className={i === 0 ? "btn btn-go" : "btn"} onClick={() => submit(s)}>{s}</button>)}
                    </div>
                  )}
                </div>
              )}
              {busy && <div className="busy" aria-live="polite">{busy}</div>}
              {ask && <ElicitSheet ask={ask} onAnswer={answer} />}
            </div>
            <button className={`ring ${ringState}`} onClick={ringPressed} aria-pressed={listening}
              aria-label={listening ? "Stop listening" : "Talk to the device"} />
          </div>
          {micNote && <p className="mic-note" role="status">{micNote}</p>}

          <form className="composer" onSubmit={(e) => { e.preventDefault(); submit(draft); }}>
            <input value={draft} onChange={(e) => setDraft(e.target.value)} placeholder={listening ? "Listening…" : "Say or type a reply"} aria-label="Request" disabled={status !== "ready"} />
            <button className="btn btn-go" disabled={!draft.trim() || !!busy || status !== "ready"}>Send</button>
          </form>
          {suggestions.length > 0 && (
            <div className="chips" aria-label="Suggested replies">
              {suggestions.map((s) => <button key={s} className="chip chip-suggest" disabled={!!busy || status !== "ready"} onClick={() => submit(s)}>{s}</button>)}
            </div>
          )}
          <details className="scenarios">
            <summary>Demo scenarios</summary>
            <div className="chips">
              {SCENARIOS.map((s) => <button key={s.label} className="chip" disabled={!!busy || status !== "ready"} onClick={() => submit(s.text)} title={s.text}>{s.label}</button>)}
            </div>
          </details>
          <ol className="transcript" aria-label="Conversation">
            {turns.slice(-10).map((t, i) => (
              <li key={i} className={t.who}>
                <span className="who">{t.who === "you" ? "You" : t.who === "notice" ? "Proactive" : "Assistant"}</span>
                <span>{t.text}{t.tool && <small className="routed">Routed to {t.tool}</small>}</span>
              </li>
            ))}
          </ol>
          <p className="status-line">{statusText}</p>
        </section>

        <Console trace={trace} calls={calls} lastData={lastData} ledgerTick={ledgerTick} bookedOutboundId={bookedOutbound}
          onChaosApplied={(note) => setTurns((t) => [...t, { who: "notice", text: `Chaos lab: ${note}.` }])} />
      </main>
    </div>
  );
}
