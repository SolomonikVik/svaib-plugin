import { lstatSync, statSync } from "node:fs";

type Stamp = [string, string | null, string | null];
type BoundaryStamp = [string, number | null];
type Part = { kind: string; text: string };

/** One current projection, reinserted for every Pi context transformation.
 * Source checks only stat paths consulted by the shared generator: no Python,
 * document reads, or filesystem traversal on an unchanged model continuation.
 */
export class MapContext {
  private main = new Map<string, string>();
  private nodes = new Map<string, string>();
  private sources = new Map<string, Stamp>();
  private boundaries: BoundaryStamp[] = [];
  ready = false;

  clear() {
    this.main.clear();
    this.nodes.clear();
    this.sources.clear();
    this.boundaries = [];
    this.ready = false;
  }
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

  boundaryChanged(): boolean {
    return this.boundaries.some(([path, kind]) => {
      const stat = /[/\\]\.svaib$/.test(path) ? lstatSync : statSync;
      try { return (stat(path).mode & 0o170000) !== kind; }
      catch (error) { return ((error as NodeJS.ErrnoException).code === "ENOENT" ? null : -1) !== kind; }
    });
  }

  apply(result: { parts?: Part[]; sourceStamp?: Stamp[]; boundaryStamp?: BoundaryStamp[] }, refreshed = false) {
    const parts = result.parts || [];
    if (refreshed) this.clear();
    if (result.boundaryStamp) { this.boundaries = result.boundaryStamp; this.ready = true; }
    if (parts.some(p => p.kind === "map")) this.main.clear();
    for (const part of parts) {
      if (part.kind !== "nodes") { this.main.set(part.kind, part.text); continue; }
      for (const text of part.text.split(/(?=## Подкарта узла `)/)) {
        const node = text.match(/^## Подкарта узла `([^`]+)\//)?.[1];
        if (node) this.nodes.set(node, text.trim());
      }
    }
    // A node read can occur just after a tool changes a source. Keep the
    // original stamp until the full snapshot has had a chance to refresh.
    for (const stamp of result.sourceStamp || []) {
      if (!this.sources.has(stamp[0])) this.sources.set(stamp[0], stamp);
    }
  }
}

export function hasContext(value: unknown, text: string): boolean {
  if (!text) return false;
  if (typeof value === "string") return value.includes(text);
  if (Array.isArray(value)) return value.some(v => hasContext(v, text));
  if (value && typeof value === "object") return Object.values(value).some(v => hasContext(v, text));
  return false;
}
