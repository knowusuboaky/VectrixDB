/**
 * Every route the client calls, in one place, so a test can check each one
 * against ../../docs/reference/openapi.json before any server runs.
 */
export type Method = "get" | "post" | "delete";

export interface Route {
  method: Method;
  path: string;
  /** Names sent in the JSON body, each of which must be a property of the request schema. */
  body?: readonly string[];
  /** Names sent as query parameters, each of which must be declared on the operation. */
  query?: readonly string[];
  /** The spec may lack this route; the client then falls back to this one. */
  fallback?: string;
}

export const routes = {
  health: { method: "get", path: "/health" },
  ready: { method: "get", path: "/ready", fallback: "/health" },
  whoami: { method: "get", path: "/auth/me" },
  collections: { method: "get", path: "/api/v1/collections" },
  describe: { method: "get", path: "/api/v1/collections/{name}" },
  createCollection: {
    method: "post",
    path: "/api/v2/collections",
    body: ["name", "dimension", "enable_text_index", "metric", "description"],
  },
  deleteCollection: { method: "delete", path: "/api/v1/collections/{name}" },
  addDocument: {
    method: "post",
    path: "/api/v1/collections/{name}/documents",
    query: ["doc_id", "metadata", "chunk", "chunk_size", "overlap"],
  },
  addTexts: {
    method: "post",
    path: "/api/v1/collections/{name}/text-upsert",
    body: ["points"],
  },
  search: {
    method: "post",
    path: "/api/v1/collections/{name}/text-search",
    body: ["query_text", "limit", "filter", "rerank"],
  },
  hybridSearch: {
    method: "post",
    path: "/api/v1/collections/{name}/text-hybrid-search",
    body: ["query_text", "limit", "filter", "rerank"],
  },
  documents: { method: "get", path: "/api/v1/collections/{name}/documents" },
  openDocument: { method: "get", path: "/api/v1/collections/{name}/documents/{doc_id}" },
  deleteDocument: { method: "delete", path: "/api/v1/collections/{name}/documents/{doc_id}" },
  sources: { method: "get", path: "/api/v1/collections/{name}/sources" },
  addSource: {
    method: "post",
    path: "/api/v1/collections/{name}/sources",
    body: ["address", "kind", "every"],
  },
  refreshSources: { method: "post", path: "/api/v1/collections/{name}/sources/refresh", body: [] },
  deleteSource: {
    method: "delete",
    path: "/api/v1/collections/{name}/sources/{source_id}",
    query: ["delete_documents"],
  },
} as const satisfies Record<string, Route>;

/** The fields of each point sent to text-upsert; a property list of TextUpsertPoint. */
export const textPointFields = ["id", "text", "payload"] as const;
