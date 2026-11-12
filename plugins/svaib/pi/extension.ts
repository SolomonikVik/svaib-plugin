import { spawn } from "node:child_process";
import { appendFileSync, mkdirSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createHash, randomUUID } from "node:crypto";
import { homedir } from "node:os";
import { kimiToolPayload } from "./kimi-schema.ts";
import { modelVisibleMcpResult } from "./mcp-result.ts";
import { usageSnapshot } from "./usage-snapshot.ts";
import { registerDelegation } from "./delegation.ts";
import { MapContext, hasContext } from "./map-context.ts";
import { Type } from "typebox";
import {
  createAgentSession, DefaultResourceLoader, SessionManager, getAgentDir, VERSION,
  type ExtensionAPI, type ExtensionContext,
} from "@earendil-works/pi-coding-agent";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const stateType = "svaib-pi-state-v1";
const contextType = "svaib-pi-context";
const oldContextTypes = new Set([contextType, "svaib-pi-map", "svaib-pi-tool-context"]);
const nodeEventTools = new Set(["read", "grep", "find", "ls", "bash", "powershell"]);
const packageVersion = JSON.parse(readFileSync(resolve(root, "package.json"), "utf8")).version;
const runtimeVersion = VERSION;

/** No shell interpolation, no credentials or model inputs in logs. */
async function python(script: string, args: string[], input: unknown, signal?: AbortSignal): Promise<any> {
  return new Promise((accept, reject) => {
    const env: NodeJS.ProcessEnv = { ...process.env, PYTHONIOENCODING: "utf-8", PLUGIN_ROOT: root };
    // Pi's package and cwd determine the policy, even when launched from Claude.
    delete env.CLAUDE_PLUGIN_ROOT;
    delete env.CLAUDE_PROJECT_DIR;
    delete env.CLAUDE_CODE_IS_COWORK;
    const child = spawn(process.env.SVAIB_PYTHON || "python3", [resolve(root, "hooks", script), ...args], {
      stdio: ["pipe", "pipe", "pipe"], env,
    });
    let stdout = "", bytes = 0, settled = false;
    const fail = (error: Error) => {
      if (settled) return;
      settled = true;
      child.kill("SIGKILL");
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      reject(error);
    };
    const abort = () => fail(new Error("svaib: hook cancelled"));
    const timer = setTimeout(() => fail(new Error(`svaib: ${script} timed out`)), 15_000);
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) abort();
    child.on("error", () => fail(new Error(`svaib: cannot start ${script}; check Python 3.10+`)));
    child.stdin.on("error", () => {});
    child.stderr.resume();
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", data => {
      bytes += Buffer.byteLength(data);
      if (bytes > 4_000_000) fail(new Error("svaib: hook output exceeded limit"));
      else stdout += data;
    });
    child.on("close", code => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      if (code !== 0) return reject(new Error(`svaib: ${script} failed (${code})`));
      try { accept(stdout.trim() ? JSON.parse(stdout) : {}); }
      catch { reject(new Error(`svaib: invalid response from ${script}`)); }
    });
    child.stdin.end(JSON.stringify(input));
  });
}

function textOf(content: any): string {
  return typeof content === "string" ? content : (content || []).filter((b: any) => b.type === "text").map((b: any) => b.text).join("\n");
}

function nativeEvent(ctx: ExtensionContext, extra: Record<string, unknown> = {}) {
  const branch = ctx.sessionManager.getBranch();
  const user = [...branch].reverse().find((e: any) => e.type === "message" && e.message.role === "user") as any;
  return {
    session_id: `pi-${ctx.sessionManager.getSessionId()}`, cwd: ctx.cwd,
    transcript_path: ctx.sessionManager.getSessionFile(), leaf_id: ctx.sessionManager.getLeafId(),
    prompt_id: user?.id, prompt: textOf(user?.message.content), pi_version: runtimeVersion,
    ...extra,
  };
}

const bridge = (mode: string, ctx: ExtensionContext, extra = {}) =>
  python("bridge_pi.py", [mode], nativeEvent(ctx, {
    ...extra,
    // BB and in-memory sessions can settle before the JSONL write is visible.
    // Keep the native branch authoritative; never log or persist this snapshot.
    ...(mode.startsWith("usage-") ? { native_entries: usageSnapshot(ctx.sessionManager.getBranch()) } : {}),
  }), mode.startsWith("usage-") ? undefined : ctx.signal);
const contextText = (result: any) => result?.hookSpecificOutput?.additionalContext || result?.context || "";

function journal(stage: string, ctx: ExtensionContext, data: Record<string, unknown>) {
  try {
    const dir = process.env.SVAIB_STATE_DIR || (process.platform === "win32"
      ? resolve(process.env.LOCALAPPDATA || resolve(homedir(), "AppData/Local"), "svaib")
      : resolve(homedir(), ".local/state/svaib"));
    mkdirSync(dir, { recursive: true });
    appendFileSync(resolve(dir, "space-map-delivery.log"), JSON.stringify({
      ts: new Date().toISOString(), harness: "pi", stage,
      session_id: `pi-${ctx.sessionManager.getSessionId()}`, ...data,
    }) + "\n", { encoding: "utf8", mode: 0o600 });
  } catch { /* Diagnostics must not break the user's turn. */ }
}

export default function svaib(pi: ExtensionAPI, options: { child?: boolean } = {}) {
  let stopPrompt = "";
  let resetSpecs = false;
  let latestText = "";
  let usagePrompt = "";
  const map = new MapContext();
  let contextKey = "";
  let request = 0;
  let generation = 0;
  let contextHash = "";
  const updateMap = async (mode: string, ctx: ExtensionContext, extra = {}, refreshed = false) => {
    const eventId = randomUUID();
    const result = await bridge(mode, ctx, { ...extra, request_id: request, event_id: eventId });
    map.apply(result, refreshed);
    if (contextText(result)) {
      generation++;
      contextHash = createHash("sha256").update(map.text).digest("hex");
      journal("generation-received", ctx, { event: mode, event_id: eventId, request_id: request, generation, hash: contextHash, chars: map.text.length });
    }
    return result;
  };
  if (!options.child) registerDelegation(pi, resolve(root, "pi/child-extension.ts"));

  // Use Pi's native MCP and its native credential store, including OAuth refresh.
  const mcp = JSON.parse(readFileSync(resolve(root, ".mcp.json"), "utf8")).mcpServers?.svaib;
  if (!mcp?.url) throw new Error("svaib: bundled MCP server is missing");
  // Foreground leaf children have a file-tool allowlist and no MCP connector.
  // Their required extension supplies policy, not another server connection.
  if (!options.child) pi.registerMcpServer("svaib", { url: mcp.url, exposure: "direct" });

  pi.on("before_provider_request", (event, ctx) => {
    journal("model-input-observed", ctx, { request_id: request, generation, hash: contextHash,
      present: hasContext(event.payload, map.text), boundary: "before_provider_request" });
    if (ctx.model?.provider === "kimi-coding") return kimiToolPayload(event.payload);
  });

  const restoreBranch = (_event: unknown, ctx: ExtensionContext) => {
    const state = [...ctx.sessionManager.getBranch()].reverse().find((e: any) => e.type === "custom" && e.customType === stateType) as any;
    stopPrompt = state?.data?.stopPrompt || "";
    latestText = "";
    usagePrompt = "";
    resetSpecs = true;
    map.clear();
    contextKey = "";
  };
  pi.on("session_start", (event, ctx) => {
    restoreBranch(event, ctx);
    ctx.ui.setStatus("svaib", `svaib ${packageVersion} · Pi`);
    pi.events.emit("subagent:acknowledge-extension", { id: "svaib-policy" });
  });
  // /tree changes the active branch without restarting the extension. Never
  // carry specification access or Stop state from an abandoned branch.
  pi.on("session_tree", restoreBranch);

  pi.on("agent_start", async (_event, ctx) => {
    latestText = "";
    const key = `${ctx.sessionManager.getSessionId()}\0${ctx.cwd}`;
    if (contextKey !== key) {
      map.clear();
      contextKey = key;
      await bridge("reset-specs", ctx);
      const initial = await updateMap("first-prompt", ctx);
      if (!initial.parts?.some((part: any) => part.kind === "map")) {
        await updateMap("compact", ctx, {}, true);
      }
      resetSpecs = false;
    }
  });

  pi.on("session_compact", () => { resetSpecs = true; });
  // Runs for automatic compaction/overflow recovery inside a turn too.
  pi.on("context", async (event, ctx) => {
    if (ctx.signal?.aborted) return;
    request++;
    // agent_start can precede persistence of the incoming user entry in BB.
    // This boundary sees the actual prompt id and closes the previous run.
    const prompt = nativeEvent(ctx).prompt_id;
    if (!options.child && prompt && prompt !== usagePrompt) {
      await bridge("usage-prompt", ctx);
      usagePrompt = prompt;
    }
    const changed = map.changed();
    if (changed.length) journal("source-invalidation", ctx, { request_id: request, generation,
      changed_count: changed.length, binding_changed: changed.some(path => /[/\\]\.svaib[/\\]platform\.json$/.test(path)) });
    if (changed.some(path => /[/\\]\.svaib[/\\]platform\.json$/.test(path))) {
      map.clear();
      resetSpecs = true;
    }
    if (resetSpecs) {
      await bridge("reset-specs", ctx);
      // Re-open node maps lost to compaction. Identity is read from shared state.
      await updateMap("compact", ctx, {}, true);
      resetSpecs = false;
    } else if (!map.text || changed.length) {
      await updateMap("refresh", ctx, { context_nodes: map.nodeIds }, true);
    }
    const text = map.text;
    if (!text) return;
    journal("emission", ctx, { request_id: request, generation, hash: contextHash, chars: text.length,
      confirmation: "context-transform-returned; provider observation logged separately" });
    return { messages: [...event.messages.filter((m: any) => !oldContextTypes.has(m.customType)), {
      role: "custom", customType: contextType, content: text, display: false, timestamp: Date.now(),
    }] };
  });

  pi.on("tool_call", async (event, ctx) => {
    if (event.toolName === "subagent") {
      return { block: true, reason: "svaib: use svaib_delegate for a fresh foreground worker/reviewer with measured partial usage. Nested/background workflows have not passed svaib acceptance." };
    }
    try {
      const result = await bridge("pre", ctx, {
        hook_event_name: "PreToolUse", tool_name: event.toolName,
        tool_input: event.input, tool_use_id: event.toolCallId,
      });
      const output = result.hookSpecificOutput;
      if (output?.permissionDecision === "deny") return { block: true, reason: output.permissionDecisionReason };
      if (output?.updatedInput) Object.assign(event.input, output.updatedInput);
    } catch (error) {
      return { block: true, reason: String(error) };
    }
  });

  pi.on("tool_result", async (event, ctx) => {
    const visible = modelVisibleMcpResult(event);
    // A failed whoami is never identity. update_me conflicts can carry the
    // current onboarding object, which the shared hook knows how to merge.
    if (event.isError && !/^mcp__(?:svaib|plugin_svaib_svaib)__update_me$/.test(event.toolName)) return visible;
    // Match the registered PostToolUse events, rather than starting Python for
    // unrelated write/delegation results that the shared map handler ignores.
    if (!nodeEventTools.has(event.toolName)
        && !/^mcp__(?:svaib|plugin_svaib_svaib)__(?:whoami|update_me)$/.test(event.toolName)) return visible;
    await updateMap("node-enter", ctx, {
      hook_event_name: "PostToolUse", tool_name: event.toolName, tool_input: event.input,
      tool_use_id: event.toolCallId,
      // Pi's structuredContent is the full original MCP CallToolResult, one
      // envelope above the server's own structuredContent. Avoid nesting again.
      tool_response: event.structuredContent ?? { content: event.content },
    });
    return visible;
  });

  pi.on("message_end", event => {
    if (event.message.role === "assistant") latestText = textOf(event.message.content);
  });
  pi.on("agent_before_settle", async (_event, ctx) => {
    const current = nativeEvent(ctx);
    if (!current.prompt_id || stopPrompt === current.prompt_id || ctx.signal?.aborted) return;
    const result = await python("check_deferred_actions.py", [], {
      // The shared hook accepts a native current-turn text via this neutral field pair.
      turn_id: current.prompt_id, last_assistant_message: latestText, stop_hook_active: false,
    }, ctx.signal);
    if (result.decision !== "block") return;
    stopPrompt = current.prompt_id;
    pi.appendEntry(stateType, { stopPrompt });
    return { entries: [{ type: "custom_message", customType: "svaib-pi-stop", content: result.reason, display: false }], continue: true };
  });
  pi.on("agent_settled", async (_event, ctx) => { if (!options.child) await bridge("usage-stop", ctx); });
  pi.on("session_shutdown", async (_event, ctx) => {
    if (!options.child) await bridge("usage-end", ctx);
  });

  pi.registerTool({
    name: "svaib_scout", label: "Скаут svaib",
    description: "Изолированный поиск места и факта в пространстве. Передай вопрос, известные адреса и границу поиска. Возвращает выжимку с адресами.",
    parameters: Type.Object({ task: Type.String({ minLength: 1 }) }),
    async execute(_id, { task }, signal, _onUpdate, ctx) {
      if (!ctx.model) throw new Error("svaib: select a model before using the scout");
      const instruction = readFileSync(resolve(root, "agents/svaib-scout.md"), "utf8").replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n/, "");
      const agentDir = getAgentDir();
      const loader = new DefaultResourceLoader({
        cwd: ctx.cwd, agentDir, noExtensions: true, disabledBuiltinExtensions: ["mcp"],
        noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
        systemPrompt: instruction,
      });
      await loader.reload();
      const { session } = await createAgentSession({
        cwd: ctx.cwd, agentDir, model: ctx.model, thinkingLevel: ctx.thinkingLevel,
        resourceLoader: loader, sessionManager: SessionManager.inMemory(ctx.cwd),
        tools: ["read", "grep", "find", "ls"],
      });
      // Provider configuration is independent of conversational context. Keep
      // extension-defined models usable without loading parent extensions.
      const providerConfig = ctx.modelRegistry.getRegisteredProviderConfig(ctx.model.provider);
      if (providerConfig) session.modelRuntime.registerProvider(ctx.model.provider, providerConfig);
      const started = Date.now();
      let turns = 0, limited = false;
      const abort = () => { void session.abort(); };
      const timer = setTimeout(() => { limited = true; abort(); }, 120_000);
      const unsubscribe = session.subscribe(event => {
        if (event.type === "turn_end" && ++turns >= 12) { limited = true; abort(); }
      });
      signal?.addEventListener("abort", abort, { once: true });
      try {
        let failed = false;
        try {
          if (!signal?.aborted) await session.prompt(task);
        } catch { failed = true; }
        const assistants = session.messages.filter((m: any) => m.role === "assistant") as any[];
        const last = assistants.at(-1);
        const total = (key: string) => assistants.every(m => typeof m.usage?.[key] === "number") ? assistants.reduce((n, m) => n + m.usage[key], 0) : null;
        const result = textOf(last?.content);
        const incomplete = failed || limited || !!signal?.aborted || !result
          || last?.stopReason === "error" || last?.stopReason === "aborted";
        return {
          isError: incomplete,
          content: [{ type: "text", text: (incomplete ? "Скаут не завершил поиск: ошибка, отмена или лимит; результат частичный.\n" : "") + result.slice(0, 40_000) }],
          details: { svaibScout: {
            id: randomUUID(), kind: "svaib-scout", models: [ctx.model.id],
            input: total("input"), output: total("output"), cache_read: total("cacheRead"), cache_write: total("cacheWrite"),
            turns: assistants.length, tools: assistants.flatMap(m => m.content).filter(b => b.type === "toolCall").length,
            seconds: Math.round((Date.now() - started) / 1000), unfinished: incomplete,
            error: failed || last?.stopReason === "error",
          } },
        };
      } finally {
        clearTimeout(timer); unsubscribe(); signal?.removeEventListener("abort", abort); session.dispose();
      }
    },
  });
}
