/** Equivalent JSON Schema spelling accepted by Kimi's tool-schema validator.
 * An empty additionalProperties schema and boolean true both allow any value.
 * Apply after Pi has serialized tools; the canonical MCP schema stays intact.
 */
const schemaKeys = new Set([
  "additionalProperties", "additionalItems", "unevaluatedProperties", "unevaluatedItems",
  "propertyNames", "contains", "not", "if", "then", "else", "contentSchema",
]);
const schemaArrayKeys = new Set(["allOf", "anyOf", "oneOf", "prefixItems"]);
const schemaMapKeys = new Set(["properties", "patternProperties", "$defs", "definitions", "dependentSchemas", "dependencies"]);

export function kimiSchema(value: any): any {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return value;
  const result = Object.fromEntries(Object.entries(value).map(([key, child]) => {
    if (key === "additionalProperties" && child !== null && typeof child === "object"
      && !Array.isArray(child) && Object.keys(child).length === 0) return [key, true];
    if (schemaMapKeys.has(key) && child !== null && typeof child === "object" && !Array.isArray(child)) {
      return [key, Object.fromEntries(Object.entries(child).map(([name, schema]) => [name, kimiSchema(schema)]))];
    }
    if ((schemaArrayKeys.has(key) || key === "items") && Array.isArray(child)) return [key, child.map(kimiSchema)];
    if (schemaKeys.has(key) || key === "items") return [key, kimiSchema(child)];
    // enum/const/default/examples and unknown annotations contain literal data.
    return [key, child];
  }));
  if (!("type" in result) && Array.isArray(result.enum) && result.enum.length) {
    // JSON Schema permits enum without type. Moonshot requires the redundant
    // type declaration; deriving it from enum does not add or remove values.
    const types = [...new Set(result.enum.map((item: any) => item === null ? "null"
      : Array.isArray(item) ? "array" : typeof item))];
    result.type = types.length === 1 ? types[0] : types;
  }
  return result;
}

export function kimiToolPayload(payload: any): any {
  if (!payload || !Array.isArray(payload.tools)) return payload;
  return { ...payload, tools: payload.tools.map((tool: any) => tool.function?.parameters ? {
    ...tool, function: { ...tool.function, parameters: kimiSchema(tool.function.parameters) },
  } : tool) };
}
