/** Pi keeps the original MCP CallToolResult in structuredContent. Models receive
 * text/image content, so expose the server's structured payload there as well.
 * Keep the original envelope: shared hooks and other extensions still need it.
 */
export function modelVisibleMcpResult<TContent extends { type: string; text?: string }, TStructured>(event: {
  toolName: string;
  content: TContent[];
  structuredContent?: TStructured;
}) {
  if (!/^mcp__(?:svaib|plugin_svaib_svaib)__/.test(event.toolName)) return;
  const envelope = event.structuredContent;
  if (!envelope || typeof envelope !== "object" || Array.isArray(envelope)) return;
  const payload = (envelope as Record<string, unknown>).structuredContent;
  if (payload === undefined || payload === null) return;
  const json = JSON.stringify(payload);
  const text = `MCP structuredContent:\n${json}`;
  // Replaying an event must not append the same payload again. A server may
  // already return its JSON in content; preserve that representation too.
  if (event.content.some(block => block.type === "text" &&
      (block.text === text || (typeof block.text === "string" && block.text.trim() === json)))) return;
  return {
    content: [...event.content, { type: "text" as const, text }],
    structuredContent: event.structuredContent,
  };
}
