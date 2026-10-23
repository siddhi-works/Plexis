# Demo video script (target 2:45, hard limit 3:00)

**Setup**
- Run `qed dev` in one terminal and `npm --prefix simulator run dev` in another.
- Open http://localhost:5173 in a 1440×900 window, with **Account linked** and **Spoken replies** on.
- Use a fresh state directory so the receipt numbers start low.

| Time | On screen | Say (voiceover) |
|---|---|---|
| 0:00–0:15 | Device idle, Plexis title | "Voice assistants are confident. But when they spend your money, confident isn't enough. Plexis is an Alexa+ add-on that only offers plans it can prove." |
| 0:15–0:45 | Click **Family trip**. The card appears: ✓ 20/20 checks, cost bar, day 1. Scroll to "Why you can trust it". | "Three people, a budget, a Thai dinner. Plexis remembers Grandma uses a wheelchair and Maya is vegetarian, and the card says so. Every rule links to the words that created it. A solver found the plan; an independent checker and a second solver proved it; it's signed." |
| 0:45–1:10 | Console **Latency** tab | "The round trip, including all seven partner servers, is a fraction of Alexa+'s 500 millisecond budget." |
| 1:10–1:40 | Click **Impossible ask**. The elicitation sheet shows the conflict and fix buttons. Click the fix. | "Ask for the impossible and you don't get a made-up plan. You get a compiler error: exactly which requirements clash, proven minimal, and the smallest change that fixes it. Every fix is re-solved before it's offered." |
| 1:40–1:55 | Type "Why not the Copper Hotel?" | "Ask why not, and Plexis forces that choice and shows which of your rules it would break." |
| 1:55–2:20 | Run Family trip, then **Book it**. Confirm with Yes. Open **Proof** and click **Tamper** on a row. | "Booking confirms the price breakdown first and can never charge twice. Here's the ledger, verified in the browser. Tamper with one receipt and the chain breaks." |
| 2:20–2:40 | **Chaos lab** → **Cancel my booked outbound flight**. A trip alert appears. | "After you book, Plexis keeps watching. The carrier cancels your flight, and Plexis has already re-planned around your hotel, every rule still proven." |
| 2:40–2:55 | Quick cuts: What to pack, Call the hotel, Voice only | "Packing and outfits from the forecast. A disclosed AI call to the front desk. Built on MCP, Amazon Bedrock and the Strands SDK. Open source." |

**Recording tips**
- Turn on spoken replies only for the clips where the device speaks.
- Trim the waits on progress notifications.
- Say in the video description that the partners are simulated.
