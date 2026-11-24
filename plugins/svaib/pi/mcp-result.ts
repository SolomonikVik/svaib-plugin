import type { SessionEntry } from "@earendil-works/pi-coding-agent";

const structuredLabel = "MCP structuredContent:\n";
export const identityTool = /^mcp__(?:svaib|plugin_svaib_svaib)__(whoami|update_me)$/;

type TextBlock = { type: string; text?: string };
const structuredPayload = (envelope: unknown) =>
  envelope && typeof envelope === "object" && !Array.isArray(envelope)
    ? (envelope as Record<string, unknown>).structuredContent : undefined;
const matchesPayload = (block: TextBlock, json: string) => block.type === "text" &&
  (block.text === structuredLabel + json || block.text?.trim() === json);

/** Pi keeps the original MCP CallToolResult in structuredContent. Models receive
 * text/image content, so expose the server's structured payload there as well.
 * Keep the original envelope: shared hooks and other extensions still need it.
 */
export function modelVisibleMcpResult<TContent extends TextBlock, TStructured>(event: {
  toolName: string;
  content: TContent[];
  structuredContent?: TStructured;
}) {
  if (!/^mcp__(?:svaib|plugin_svaib_svaib)__/.test(event.toolName)) return;
  const payload = structuredPayload(event.structuredContent);
  if (payload === undefined || payload === null) return;
  const json = JSON.stringify(payload);
  const text = structuredLabel + json;
  // Replaying an event must not append the same payload again. A server may
  // already return its JSON in content; preserve that representation too.
  if (event.content.some(block => matchesPayload(block, json))) return;
  return {
    content: [...event.content, { type: "text" as const, text }],
    structuredContent: event.structuredContent,
  };
}

export function identityDetails(scope: string, details: unknown, envelope: unknown, content: TextBlock[]) {
  const payload = structuredPayload(envelope);
  const json = payload == null ? undefined : JSON.stringify(payload);
  const index = json === undefined ? -1 : content.findIndex(block => matchesPayload(block, json));
  return { ...(details as object), svaibIdentityScope: scope, svaibStructuredIndex: index < 0 ? undefined : index };
}

/** Project only native MCP results and their matching call arguments. */
export function identityHistory(branch: SessionEntry[]) {
  const history = [];
  const calls = new Map<string, { name: string; arguments: unknown }>();
  for (const entry of branch) {
    if (entry.type !== "message") continue;
    const message = entry.message;
    if (message.role === "assistant") {
      calls.clear();
      for (const block of message.content) {
        if (block.type === "toolCall" && identityTool.test(block.name)) calls.set(block.id, block);
      }
    }
    if (message.role !== "toolResult") continue;
    const call = calls.get(message.toolCallId);
    const operation = identityTool.exec(message.toolName)?.[1];
    if (!operation || call?.name !== message.toolName || (message.isError && operation === "whoami")) continue;
    const details = message.details as { server?: string; tool?: string; svaibIdentityScope?: string;
      svaibStructuredIndex?: number } | undefined;
    if (details?.server !== "svaib" || details.tool !== operation) continue;
    calls.delete(message.toolCallId);
    // Native Pi persists model-visible content, without the transient envelope.
    const structured = details.svaibStructuredIndex === undefined
      ? message.content.findLast(b => b.type === "text" && b.text.startsWith(structuredLabel))
      : message.content[details.svaibStructuredIndex];
    let response: unknown = message.content.filter(b => b.type === "text");
    if (structured?.type === "text") {
      const json = structured.text.startsWith(structuredLabel) ? structured.text.slice(structuredLabel.length) : structured.text;
      try { response = { structuredContent: JSON.parse(json) }; }
      catch { continue; }
    } else if (message.isError) {
      // An error without structured server data can't update the cache.
      continue;
    }
    history.push({ tool_name: message.toolName, tool_input: call.arguments, tool_response: response,
      scope: details.svaibIdentityScope });
  }
  return history;
}
