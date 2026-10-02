/* What the retrieval service answers, as JSDoc, so an editor can help and a
   reader can see the shape without opening the service. A convenience, not a
   contract: the service's own OpenAPI document is the contract, and it ships
   with the library as docs/reference/openapi.json.

   A collection carrying an entitlement policy answers with its settings and
   withholds every number that moves when documents are written, so the
   counting fields below are absent for one. */

/**
 * @typedef {object} Collection A row of GET /api/v1/collections.
 * @property {string} name
 * @property {number} dimension
 * @property {string} metric
 * @property {number} [count] absent under a policy
 * @property {string} [updated_at]
 * @property {string[]} [tags] dense, hybrid, ultimate or graph
 * @property {boolean} [entitlement_policy] true, and counts withheld
 */

/**
 * @typedef {object} Health GET /api/v1/collections/{name}/health.
 * @property {string} name
 * @property {boolean} policied
 * @property {number} [count]
 * @property {string} [updated_at]
 * @property {string} [state] healthy, or rebuild
 * @property {string} [advice] what to do about the state
 * @property {string} [index_build_id] the build the index stands on
 * @property {string} [embedding_model]
 * @property {{dense: boolean, keyword: boolean, hybrid: boolean, graph: boolean}} capabilities
 */

/**
 * @typedef {object} Build A row of GET /api/v1/collections/{name}/builds.
 * @property {string} build_id
 * @property {number} chunks still in the collection from that build
 * @property {boolean} current
 * @property {string} [written_at] when it first wrote
 * @property {number} [quality] its chunks' mean extraction quality
 * @property {number} low how many read below the line
 */

/**
 * @typedef {object} Growth GET /api/v1/collections/{name}/growth.
 * @property {string[]} days UTC dates, today last
 * @property {number[]} written chunks first written on each day
 * @property {number} before chunks older than the first day
 */

/**
 * @typedef {object} Quality GET /api/v1/collections/{name}/quality.
 * @property {number} threshold
 * @property {number} scored
 * @property {number} unscored
 * @property {number} below
 * @property {number[]} bins twenty, from 0 to 1
 * @property {{id: string, quality: number, below_line: boolean, reasons: string[], text?: string}[]} worst
 */

/**
 * @typedef {object} Points GET /api/v1/collections/{name}/points.
 * @property {string[]} ids
 * @property {number} total as this caller may see it
 * @property {number} limit
 * @property {number} offset
 * @property {boolean} [text_hidden] the caller may list ids and not read them
 */

/**
 * @typedef {object} Provenance GET /api/v1/collections/{name}/provenance/{id}.
 * @property {boolean} present
 * @property {string} [build_id]
 * @property {string} [source]
 * @property {number} [page]
 * @property {number} [quality]
 * @property {string} [text] the first words, when the caller may read them
 */

/**
 * @typedef {object} Run A retrieval or chunking run: GET /api/v1/evaluations/{id} or /api/v1/chunking/{id}.
 * @property {string} id
 * @property {string} created_at
 * @property {{source: string, sha256: string, questions: number}} golden
 * @property {string[]} targets
 * @property {Record<string, string>} picks what each rule chose
 * @property {object[]} setups every way that was asked, with its numbers
 */

export {}
