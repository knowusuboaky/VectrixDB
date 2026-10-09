// Checks every route the client uses against ../../docs/reference/openapi.json, so a renamed
// route or body field fails here before any server runs.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { routes, textPointFields, type Route } from "../src/routes.js";

const here = dirname(fileURLToPath(import.meta.url));
const spec = JSON.parse(readFileSync(join(here, "..", "..", "..", "docs", "reference", "openapi.json"), "utf8")) as {
  paths: Record<string, Record<string, Operation>>;
  components: { schemas: Record<string, Schema> };
};

interface Schema {
  properties?: Record<string, unknown>;
  $ref?: string;
  anyOf?: Schema[];
}

interface Operation {
  parameters?: { name: string; in: string }[];
  requestBody?: { content?: { "application/json"?: { schema?: Schema } } };
}

function resolve(schema: Schema | undefined): Schema | undefined {
  if (!schema) return undefined;
  if (schema.$ref) {
    const name = schema.$ref.replace("#/components/schemas/", "");
    return resolve(spec.components.schemas[name]);
  }
  if (schema.anyOf) return resolve(schema.anyOf.find((s) => s.$ref ?? s.properties));
  return schema;
}

for (const [name, route] of Object.entries(routes as Record<string, Route>)) {
  test(`${name}: ${route.method.toUpperCase()} ${route.path}`, () => {
    let item = spec.paths[route.path];
    if (!item && route.fallback) {
      item = spec.paths[route.fallback];
      assert.ok(item, `${route.path} is absent and so is its fallback ${route.fallback}`);
      return;
    }
    assert.ok(item, `${route.path} is not in openapi.json`);
    const op = item[route.method];
    assert.ok(op, `${route.method.toUpperCase()} ${route.path} is not in openapi.json`);

    const declared = new Set((op.parameters ?? []).filter((p) => p.in === "query").map((p) => p.name));
    for (const q of route.query ?? []) {
      assert.ok(declared.has(q), `query parameter ${q} is not declared on ${route.path}`);
    }

    if (route.body) {
      const schema = resolve(op.requestBody?.content?.["application/json"]?.schema);
      assert.ok(schema?.properties, `${route.path} has no JSON request schema`);
      for (const field of route.body) {
        assert.ok(field in schema.properties, `body field ${field} is not in the schema for ${route.path}`);
      }
    }
  });
}

test("text-upsert points carry id, text and payload", () => {
  const point = resolve(spec.components.schemas.TextUpsertPoint);
  assert.ok(point?.properties);
  for (const field of textPointFields) {
    assert.ok(field in point.properties, `TextUpsertPoint lacks ${field}`);
  }
});
