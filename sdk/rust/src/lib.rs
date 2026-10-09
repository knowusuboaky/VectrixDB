//! Client for VectrixDB: collections, documents, text search, sources.
//!
//! ```no_run
//! use vectrixdb::{Client, SearchOptions};
//!
//! # async fn run() -> vectrixdb::Result<()> {
//! let db = Client::new("http://127.0.0.1:8000").key("...").build()?;
//! for hit in db.search("docs", "refunds", SearchOptions::default()).await? {
//!     println!("{} ({:.2}): {}", hit.citation, hit.score, hit.text);
//! }
//! # Ok(())
//! # }
//! ```
//!
//! Every call is an async method on [`Client`]; the `blocking` feature adds
//! [`BlockingClient`] with the same calls, synchronous.
//!
//! [`ClientBuilder`] also has what a company network asks for:
//! [`allow_http`](ClientBuilder::allow_http) (a key or token over plain
//! `http://` to another machine is refused without it),
//! [`key_header`](ClientBuilder::key_header),
//! [`token_header`](ClientBuilder::token_header),
//! [`header`](ClientBuilder::header), [`prefix`](ClientBuilder::prefix),
//! [`gateway_paths`](ClientBuilder::gateway_paths),
//! [`ca_certificate`](ClientBuilder::ca_certificate) and
//! [`identity`](ClientBuilder::identity). The client never follows a
//! redirect, and never prints the key or token.

mod client;
mod error;
mod gateway;
pub mod generated;
mod types;

#[cfg(feature = "blocking")]
mod blocking;

#[cfg(feature = "blocking")]
pub use blocking::BlockingClient;
pub use client::{Client, ClientBuilder};
pub use error::{Error, Kind, Result};
pub use gateway::GatewayPaths;
pub use types::{
    AddOptions, Added, Collection, CreateOptions, Document, Hit, Mode, Refreshed, SearchOptions,
    Source, SourceOptions, Text,
};

/// Asserts the routes and body fields the client uses exist in `docs/reference/openapi.json`.
#[cfg(test)]
mod spec_check {
    use serde_json::Value;

    use crate::client::routes;
    use crate::generated::*;

    fn spec() -> Value {
        let path = concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../docs/reference/openapi.json"
        );
        serde_json::from_str(&std::fs::read_to_string(path).expect("docs/reference/openapi.json"))
            .unwrap()
    }

    #[test]
    fn every_route_is_in_the_spec() {
        let spec = spec();
        for (method, path) in routes::ALL {
            let op = spec["paths"][*path][*method].as_object();
            assert!(op.is_some(), "{} {} is not in openapi.json", method, path);
        }
    }

    #[test]
    fn query_parameters_are_in_the_spec() {
        let spec = spec();
        let params = |route: (&str, &str)| -> Vec<String> {
            spec["paths"][route.1][route.0]["parameters"]
                .as_array()
                .unwrap()
                .iter()
                .filter(|p| p["in"] == "query")
                .map(|p| p["name"].as_str().unwrap().to_owned())
                .collect()
        };
        let upload = params(routes::ADD_DOCUMENT);
        for name in ["doc_id", "metadata", "chunk", "chunk_size", "overlap"] {
            assert!(
                upload.contains(&name.to_owned()),
                "add_document query {name}"
            );
        }
        assert!(params(routes::DELETE_SOURCE).contains(&"delete_documents".to_owned()));
    }

    /// Each request body, serialised with every field set, has only fields
    /// the schema lists, and all the ones it requires.
    #[test]
    fn body_fields_are_in_the_spec() {
        let spec = spec();
        let bodies: Vec<(&str, (&str, &str), Value)> = vec![
            (
                "CreateCollectionRequestV2",
                routes::CREATE,
                serde_json::to_value(CreateCollectionRequestV2 {
                    name: "n".into(),
                    dimension: 1,
                    enable_text_index: Some(true),
                    metric: Some("cosine".into()),
                    description: Some("d".into()),
                    hnsw_m: Some(16),
                    hnsw_ef_construction: Some(200),
                    tags: Some(vec![]),
                })
                .unwrap(),
            ),
            (
                "TextUpsertRequest",
                routes::ADD_TEXTS,
                serde_json::to_value(TextUpsertRequest {
                    points: vec![TextUpsertPoint {
                        id: "i".into(),
                        text: "t".into(),
                        payload: Some(Default::default()),
                    }],
                })
                .unwrap(),
            ),
            (
                "TextSearchRequest",
                routes::SEARCH,
                serde_json::to_value(TextSearchRequest {
                    query_text: "q".into(),
                    limit: Some(1),
                    filter: Some(Default::default()),
                    rerank: Some(true),
                    include_vectors: Some(false),
                    score_threshold: Some(0.0),
                    use_cache: Some(true),
                })
                .unwrap(),
            ),
            (
                "AddSourceRequest",
                routes::ADD_SOURCE,
                serde_json::to_value(AddSourceRequest {
                    address: "a".into(),
                    kind: Some("feed".into()),
                    every: Some("6h".into()),
                    articles: Some(true),
                    delete_when_gone: Some(true),
                    transcribe: Some(true),
                })
                .unwrap(),
            ),
            (
                "RefreshRequest",
                routes::REFRESH,
                serde_json::to_value(RefreshRequest {
                    force: Some(true),
                    max_items: Some(1),
                    source: Some("s".into()),
                })
                .unwrap(),
            ),
        ];
        for (schema_name, route, body) in bodies {
            let schema = &spec["components"]["schemas"][schema_name];
            let properties = schema["properties"].as_object().unwrap();
            for field in body.as_object().unwrap().keys() {
                assert!(properties.contains_key(field), "{schema_name}.{field}");
            }
            for required in schema["required"].as_array().into_iter().flatten() {
                assert!(
                    body.get(required.as_str().unwrap()).is_some(),
                    "{schema_name} requires {required}"
                );
            }
            // The route's body is that schema, or an anyOf holding it.
            let referenced = spec["paths"][route.1][route.0]["requestBody"]["content"]
                ["application/json"]["schema"]
                .to_string();
            assert!(
                referenced.contains(&format!("#/components/schemas/{schema_name}\"")),
                "{route:?}"
            );
        }
        // The hybrid route takes the same body as the meaning one.
        let hybrid = spec["paths"][routes::HYBRID.1]["post"]["requestBody"].to_string();
        assert!(hybrid.contains("TextSearchRequest"));
        // TextUpsertPoint is what the points list holds.
        let items =
            &spec["components"]["schemas"]["TextUpsertRequest"]["properties"]["points"]["items"];
        assert_eq!(items["$ref"], "#/components/schemas/TextUpsertPoint");
    }

    #[test]
    fn user_agent_names_the_crate_version() {
        assert_eq!(crate::client::USER_AGENT, "vectrixdb-rust/2.2.0");
    }
}
