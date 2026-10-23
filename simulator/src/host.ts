// The simulated Alexa+ orchestration layer: picks which add-on tool serves an
// utterance and fills its arguments from conversation context. Real Alexa+ does
// this with an LLM over tool descriptions; here it is deterministic and shown in
// the trace so the routing decision is inspectable. The trip conversation itself
// (questions, suggestions, "the first one") is understood by the add-on.

export type HostContext = {
  planId?: string;
  draftId?: string;
  tripId?: string;
  receiptIndex?: number;
  lastStatus?: string;
};
export type Route = { tool: string; args: Record<string, unknown>; why: string };

const has = (s: string, re: RegExp) => re.test(s.toLowerCase());
const BOOK_IT = /^\s*(?:yes,?\s*|ok(?:ay)?,?\s*|great,?\s*)?(?:please\s+)?(?:book|reserve|lock in|confirm)(?:\s+(?:it|this|that|the trip|this trip|that one|it please))?\s*[.!]?\s*$/i;

export function route(utterance: string, ctx: HostContext): Route {
  const u = utterance.trim();
  const plan = ctx.planId;
  const talking = ctx.draftId && (ctx.lastStatus === "needs_input" || ctx.lastStatus === "infeasible");
  const trip = (why: string) => ({ tool: "plan_trip", args: { request: u, ...(ctx.draftId ? { draft_id: ctx.draftId } : {}) }, why });

  if (has(u, /^\s*(?:please\s+)?(?:remember|don'?t forget)\b/)) return { tool: "household_memory", args: { action: "remember", statement: u.replace(/^(please\s+)?(remember|don'?t forget)\s+(that\s+)?/i, "") }, why: "a fact to remember" };
  if (has(u, /\b(forget everything|forget all)\b/)) return { tool: "household_memory", args: { action: "forget_all" }, why: "erase household memory" };
  if (has(u, /^\s*forget\b/)) return { tool: "household_memory", args: { action: "forget", statement: u.replace(/^.*?forget\s+(that\s+)?/i, "") }, why: "remove a remembered fact" };
  if (has(u, /\bwhat do you (know|remember)\b/)) return { tool: "household_memory", args: { action: "list" }, why: "list household memory" };
  // Mid-conversation, every reply belongs to the trip conversation.
  if (talking) return trip("answer to the add-on's question");
  if (has(u, /\b(rebook|switch (me|us) to)\b/)) return { tool: "trip_status", args: { accept_rebooking: true, ...(ctx.tripId ? { trip_id: ctx.tripId } : {}) }, why: "accept the prepared rebooking" };
  if (has(u, /\b(trip status|any (updates|changes)|is my (flight|trip)|flight status)\b/)) return { tool: "trip_status", args: ctx.tripId ? { trip_id: ctx.tripId } : {}, why: "check booked trip" };
  if (has(u, /\b(verify|prove|is (it|this) (real|authentic))\b/) && ctx.receiptIndex !== undefined)
    return { tool: "verify_receipt", args: { receipt_index: ctx.receiptIndex }, why: "independent receipt check" };
  if (plan) {
    if (BOOK_IT.test(u)) return { tool: "book_trip", args: { plan_id: plan }, why: "customer asked to book this plan" };
    if (has(u, /\bwhy\b/)) return { tool: "explain_choice", args: { plan_id: plan, question: u }, why: "why / why-not question" };
    if (has(u, /\b(weather|better dates|rain|sunnier|drier)\b/)) return { tool: "check_better_dates", args: { plan_id: plan }, why: "weather-aware dates" };
    if (has(u, /\b(pack|wear|outfit|bring|brief|prepare)\b/)) return { tool: "travel_brief", args: { plan_id: plan }, why: "packing and outfits" };
    if (has(u, /\b(call|ask the (hotel|front desk)|phone)\b/)) {
      const qs = u.replace(/^.*?\b(call|ask)\b( the)?( hotel| front desk)?( and ask)?( if| whether)?/i, "").split(/\band\b|,|\?/).map((q) => q.trim()).filter((q) => q.length > 3);
      return { tool: "call_front_desk", args: { plan_id: plan, questions: qs.length ? qs : ["Can we check in early?"] }, why: "questions for the property" };
    }
  }
  return trip(ctx.draftId ? "trip conversation" : "new trip request");
}
