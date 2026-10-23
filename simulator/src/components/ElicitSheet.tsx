// Renders an MCP elicitation (form mode) the way a voice device would:
// the question is spoken, enum choices become large buttons, booleans become yes/no.
import { useState } from "react";
import type { ElicitAnswer, ElicitAsk } from "../mcp";

export function ElicitSheet({ ask, onAnswer }: { ask: ElicitAsk; onAnswer: (a: ElicitAnswer) => void }) {
  const props = ask.schema.properties ?? {};
  const keys = Object.keys(props);
  const [values, setValues] = useState<Record<string, unknown>>(() =>
    Object.fromEntries(keys.map((k) => [k, props[k].default ?? (props[k].type === "boolean" ? false : props[k].enum ? props[k].enum[0] : "")])),
  );
  const single = keys.length === 1 ? keys[0] : null;
  const onlyBool = single && props[single].type === "boolean";
  const onlyEnum = single && Array.isArray(props[single].enum);

  const accept = (content: Record<string, unknown>) => onAnswer({ action: "accept", content });

  return (
    <div className="sheet" role="dialog" aria-modal="true" aria-label="The add-on is asking you something">
      <p className="sheet-q">{ask.message}</p>
      {onlyBool && (
        <div className="sheet-actions">
          <button className="btn btn-go" autoFocus onClick={() => accept({ [single!]: true })}>Yes</button>
          <button className="btn" onClick={() => onAnswer({ action: "decline" })}>No</button>
        </div>
      )}
      {onlyEnum && (
        <div className="sheet-choices">
          {(props[single!].enum as string[]).map((v, i) => (
            <button key={v} className={i === 0 ? "btn btn-go" : "btn"} autoFocus={i === 0} onClick={() => accept({ [single!]: v })}>{v}</button>
          ))}
          <button className="btn btn-quiet" onClick={() => onAnswer({ action: "decline" })}>Never mind</button>
        </div>
      )}
      {!onlyBool && !onlyEnum && (
        <form className="sheet-form" onSubmit={(e) => { e.preventDefault(); accept(values); }}>
          {keys.map((k) => {
            const p = props[k];
            const label = p.description ?? p.title ?? k;
            if (Array.isArray(p.enum)) {
              return (
                <label key={k}><span>{label}</span>
                  <select value={String(values[k])} onChange={(e) => setValues({ ...values, [k]: e.target.value })}>
                    {p.enum.map((v: string) => <option key={v} value={v}>{v}</option>)}
                  </select>
                </label>
              );
            }
            const numeric = p.type === "integer" || p.type === "number";
            return (
              <label key={k}><span>{label}</span>
                <input
                  type={numeric ? "number" : "text"} min={p.minimum} max={p.maximum}
                  value={String(values[k] ?? "")}
                  onChange={(e) => setValues({ ...values, [k]: numeric ? Number(e.target.value) : e.target.value })}
                />
              </label>
            );
          })}
          <div className="sheet-actions">
            <button className="btn btn-go" type="submit">Continue</button>
            <button className="btn btn-quiet" type="button" onClick={() => onAnswer({ action: "cancel" })}>Cancel</button>
          </div>
        </form>
      )}
    </div>
  );
}
