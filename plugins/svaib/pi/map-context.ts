import { statSync } from "node:fs";

type Stamp = [string, string | null, string | null];
type Part = { kind: string; text: string };

/** One current projection, reinserted for every Pi context transformation.
 * Source checks only stat paths consulted by the shared generator: no Python,
 * document reads, or filesystem traversal on an unchanged model continuation.
 */
export class MapContext {
  private main = new Map<string, string>();
  private nodes = new Map<string, string>();
  private sources = new Map<string, Stamp>();

  clear() { this.main.clear(); this.nodes.clear(); this.sources.clear(); }
  get nodeIds() { return [...this.nodes.keys()]; }
  get text() { return [...["map", "profile", "goals"].map(k => this.main.get(k)).filter(Boolean), ...this.nodes.values()].join("\n\n"); }

  changed(): string[] {
    return [...this.sources.values()].filter(([path, time, size]) => {
      try {
        const now = statSync(path, { bigint: true });
        return now.mtimeNs.toString() !== time || now.size.toString() !== size;
      } catch { return time !== null || size !== null; }
    }).map(([path]) => path);
  }

  apply(result: { parts?: Part[]; sourceStamp?: Stamp[] }, refreshed = false) {
    const parts = result.parts || [];
    if (parts.some(p => p.kind === "map")) this.main.clear();
    if (refreshed) { this.nodes.clear(); this.sources.clear(); }
    for (const part of parts) {
      if (part.kind !== "nodes") { this.main.set(part.kind, part.text); continue; }
      for (const text of part.text.split(/(?=## Подкарта узла `)/)) {
        const node = text.match(/^## Подкарта узла `([^`]+)\//)?.[1];
        if (node) this.nodes.set(node, text.trim());
      }
    }
    for (const stamp of result.sourceStamp || []) this.sources.set(stamp[0], stamp);
  }
}

export function hasContext(value: unknown, text: string): boolean {
  if (!text) return false;
  if (typeof value === "string") return value.includes(text);
  if (Array.isArray(value)) return value.some(v => hasContext(v, text));
  if (value && typeof value === "object") return Object.values(value).some(v => hasContext(v, text));
  return false;
}
