// Browser voice I/O (Web Speech API), with turn-taking:
// - speak() resolves when the reply has finished, so the app can listen again (hands-free);
// - listen() reports why it stopped (no speech, mic blocked) instead of failing silently;
// - stopSpeaking() lets the customer interrupt.
// The voice is pluggable: swap these two functions for Amazon Polly / Transcribe or a custom voice agent.

export type ListenEnd = "done" | "no-speech" | "not-allowed" | "unsupported" | "aborted" | "error";

export function speechSupported(): boolean {
  return typeof window !== "undefined" && ("SpeechRecognition" in window || "webkitSpeechRecognition" in window);
}

export function listen(onText: (text: string, final: boolean) => void, onEnd: (why: ListenEnd) => void): () => void {
  const Ctor: any = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
  if (!Ctor) {
    onEnd("unsupported");
    return () => {};
  }
  const rec: any = new Ctor();
  rec.lang = "en-US";
  rec.interimResults = true;
  rec.continuous = false;
  rec.maxAlternatives = 1;
  let ended = false;
  let heard = false;
  const finish = (why: ListenEnd) => {
    if (!ended) {
      ended = true;
      onEnd(why);
    }
  };
  rec.onresult = (ev: any) => {
    let text = "";
    let final = false;
    for (let i = ev.resultIndex; i < ev.results.length; i++) {
      text += ev.results[i][0].transcript;
      final = final || ev.results[i].isFinal;
    }
    heard = heard || text.trim().length > 0;
    onText(text, final);
  };
  rec.onerror = (ev: any) => finish(ev.error === "no-speech" ? "no-speech" : ev.error === "not-allowed" || ev.error === "service-not-allowed" ? "not-allowed" : ev.error === "aborted" ? "aborted" : "error");
  rec.onend = () => finish(heard ? "done" : "no-speech");
  try {
    rec.start();
  } catch {
    finish("error");
  }
  return () => {
    ended = true;
    try {
      rec.abort();
    } catch {
      /* already stopped */
    }
  };
}

let voices: SpeechSynthesisVoice[] = [];
if (typeof window !== "undefined" && "speechSynthesis" in window) {
  const load = () => (voices = window.speechSynthesis.getVoices());
  load();
  window.speechSynthesis.addEventListener?.("voiceschanged", load);
}

function pickVoice(): SpeechSynthesisVoice | undefined {
  const en = voices.filter((v) => /^en(-|_)US/i.test(v.lang));
  return en.find((v) => /(Aria|Jenny|Samantha|Google US English|Natural)/i.test(v.name)) ?? en[0];
}

/** Speak a reply; resolves when it finishes or is interrupted. */
export function speak(text: string, enabled: boolean): Promise<void> {
  if (!enabled || typeof window === "undefined" || !("speechSynthesis" in window) || !text.trim()) return Promise.resolve();
  window.speechSynthesis.cancel();
  return new Promise((resolve) => {
    const u = new SpeechSynthesisUtterance(text.replace(/\$([\d,]+)/g, "$1 dollars"));
    u.rate = 1.06;
    const v = pickVoice();
    if (v) u.voice = v;
    u.onend = () => resolve();
    u.onerror = () => resolve();
    window.speechSynthesis.speak(u);
    // Some browsers never fire onend for long text; don't hang the conversation.
    setTimeout(resolve, Math.min(30000, 2500 + text.length * 70));
  });
}

export function stopSpeaking(): void {
  if (typeof window !== "undefined" && "speechSynthesis" in window) window.speechSynthesis.cancel();
}

export function isSpeaking(): boolean {
  return typeof window !== "undefined" && "speechSynthesis" in window && window.speechSynthesis.speaking;
}

const YES = /^\s*(yes|yeah|yep|sure|ok(ay)?|go ahead|do it|confirm|book it|please do)\b/i;
const NO = /^\s*(no|nope|nah|cancel|stop|don'?t|never mind)\b/i;
const ORD: Record<string, number> = { first: 0, "1st": 0, second: 1, "2nd": 1, third: 2, "3rd": 2, last: -1 };

/** Turn a spoken reply into an answer for an on-screen question (yes/no, or one of the options). */
export function matchSpoken(text: string, options: string[] | null, boolean: boolean): { value?: string | boolean; decline?: boolean } {
  if (boolean) {
    if (YES.test(text)) return { value: true };
    if (NO.test(text)) return { decline: true };
    return {};
  }
  if (!options?.length) return {};
  const low = text.toLowerCase();
  for (const [w, i] of Object.entries(ORD)) if (new RegExp(`\\b${w}\\b`).test(low)) return { value: options.at(i) };
  const hit = options.find((o) => low.includes(o.toLowerCase()) || o.toLowerCase().includes(low.trim()));
  if (hit) return { value: hit };
  if (NO.test(text)) return { decline: true };
  return {};
}
