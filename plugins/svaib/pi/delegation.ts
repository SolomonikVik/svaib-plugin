/** Thin adapter to the ready executor's public structured delegation API.
 * Its ordinary tool drops receipts on thrown failures; terminal events retain
 * partial usage and correlate cancellation with the exact foreground child.
 */
import { randomUUID } from "node:crypto";
import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import subagents from "pi-subagents";
import { registerRequiredChildExtensions } from "pi-subagents/required-child-extensions";
import { SUBAGENT_DELEGATION_REQUEST_EVENT as REQUEST, SUBAGENT_DELEGATION_RESPONSE_EVENT as RESPONSE,
  SUBAGENT_DELEGATION_CANCEL_EVENT as CANCEL, type SubagentDelegationResponse } from "pi-subagents/delegation";

export function registerDelegation(pi: ExtensionAPI, policyPath: string) {
  subagents(pi);
  let required: { dispose(): void } | undefined;
  pi.on("session_start", (_event, ctx) => {
    required?.dispose();
    required = registerRequiredChildExtensions({
      sessionId: ctx.sessionManager.getSessionId(), requireForAllRunners: true,
      extensions: [{ id: "svaib-policy", path: policyPath }],
    });
    // The accepted contract is a fresh foreground leaf. Keep package-owned
    // background/workflow tools outside the model's ordinary tool inventory.
    pi.setActiveTools(pi.getActiveTools().filter(name => !["subagent", "bg_wait"].includes(name)));
  });
  pi.on("session_shutdown", () => { required?.dispose(); required = undefined; });

  pi.registerTool({
    name: "svaib_delegate", label: "Исполнитель svaib",
    description: "Отдельный исполнитель или независимый reviewer через pi-subagents. Передай имя агента (worker/reviewer), задачу, адреса и границу. История родителя не передаётся; svaib hooks обязательны. Результат с расходом возвращается после завершения, в том числе при ошибке/отмене.",
    parameters: Type.Object({ agent: Type.String({ minLength: 1 }), task: Type.String({ minLength: 1 }) }),
    async execute(id, { agent, task }, signal, _onUpdate, ctx) {
      const identity = { requestId: randomUUID(), ownerRunId: ctx.sessionManager.getSessionId(), nodeId: id };
      let unsubscribe = () => {};
      const cancel = () => pi.events.emit(CANCEL, identity);
      try {
        const response = await new Promise<SubagentDelegationResponse>(resolve => {
          unsubscribe = pi.events.on(RESPONSE, payload => {
            const answer = payload as SubagentDelegationResponse;
            if (answer.requestId === identity.requestId && answer.ownerRunId === identity.ownerRunId
                && answer.nodeId === identity.nodeId) resolve(answer);
          });
          signal?.addEventListener("abort", cancel, { once: true });
          pi.events.emit(REQUEST, { ...identity, agent, task, cwd: ctx.cwd, context: "fresh", result: { kind: "text" } });
          if (signal?.aborted) cancel();
        });
        const terminal = response as Exclude<SubagentDelegationResponse, { status: "invalid_request" }>;
        const u = terminal.usage;
        const success = response.status === "completed";
        const result = terminal.result?.kind === "text" ? terminal.result.text : "";
        return { isError: !success,
          content: [{ type: "text", text: ((success ? "" : `Исполнитель ${agent}: ${response.status}. ${response.error || ""}\n`) + result).slice(0, 40_000) }],
          details: { svaibAgents: [{ id: identity.requestId, kind: agent,
            models: terminal.model ? [terminal.model] : [],
            input: u?.input ?? null, output: u?.output ?? null, cache_read: u?.cacheRead ?? null, cache_write: u?.cacheWrite ?? null,
            turns: u?.turns ?? null, tools: u?.toolCalls ?? null, seconds: u ? u.durationMs / 1000 : null,
            error: !success, unfinished: ["cancelled", "interrupted", "timed_out"].includes(response.status),
          }] },
        };
      } finally {
        unsubscribe(); signal?.removeEventListener("abort", cancel);
      }
    },
  });
}
