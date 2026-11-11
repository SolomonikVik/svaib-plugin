/** The ledger needs branch identity, calls and receipts, never conversation text.
 * Compacted sessions retain raw tool output; exclude it before JSON serialization.
 */
export function usageSnapshot(entries: readonly any[]): any[] {
  return entries.map(entry => {
    const out: any = { id: entry.id, parentId: entry.parentId, type: entry.type, timestamp: entry.timestamp };
    const message = entry.message;
    if (entry.type !== "message" || !message) return out;
    out.message = { role: message.role };
    if (message.role === "assistant") {
      Object.assign(out.message, { model: message.model, usage: message.usage, stopReason: message.stopReason,
        content: (message.content || []).filter((b: any) => b.type === "toolCall").map((b: any) => ({
          type: b.type, id: b.id, name: b.name,
          arguments: ["read", "bash", "powershell"].includes(b.name) ? b.arguments : {},
        })) });
    } else if (message.role === "toolResult") {
      const details = message.details || {};
      out.message.toolName = message.toolName;
      out.message.details = {
        ...(details.svaibScout ? { svaibScout: details.svaibScout } : {}),
        ...(details.svaibAgents ? { svaibAgents: details.svaibAgents } : {}),
      };
    }
    return out;
  });
}
