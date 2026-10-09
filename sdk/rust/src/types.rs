//! What the calls return, and the options they take.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

type Extra = Map<String, Value>;

/// One collection, as `GET /api/v1/collections/{name}` describes it.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Collection {
    pub name: String,
    #[serde(default)]
    pub dimension: Option<u64>,
    #[serde(default)]
    pub metric: Option<String>,
    #[serde(default)]
    pub count: u64,
    #[serde(default)]
    pub size_bytes: u64,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub has_text_index: bool,
    #[serde(default)]
    pub tags: Vec<String>,
    #[serde(default)]
    pub created_at: Option<String>,
    #[serde(default)]
    pub updated_at: Option<String>,
    #[serde(default)]
    pub indexed_fields: Vec<Value>,
    /// Everything else the server put on the object.
    #[serde(flatten)]
    pub extra: Extra,
}

/// One search result.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Hit {
    pub id: String,
    #[serde(default)]
    pub score: f64,
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub metadata: Map<String, Value>,
    /// Where the text came from: `metadata["_vx_citation"]`, else
    /// `metadata["source"]`, else the id.
    #[serde(skip)]
    pub citation: String,
    #[serde(flatten)]
    pub extra: Extra,
}

impl Hit {
    pub(crate) fn with_citation(mut self) -> Hit {
        let from = |key: &str| {
            self.metadata
                .get(key)
                .and_then(Value::as_str)
                .map(str::to_owned)
        };
        self.citation = from("_vx_citation")
            .or_else(|| from("source"))
            .unwrap_or_else(|| self.id.clone());
        self
    }
}

/// One kept document, as `GET /api/v1/collections/{name}/documents` lists it.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Document {
    pub doc_id: String,
    #[serde(default)]
    pub filename: Option<String>,
    #[serde(default)]
    pub kind: Option<String>,
    #[serde(default)]
    pub source: Option<Value>,
    #[serde(default)]
    pub version: Option<String>,
    #[serde(default)]
    pub extracted_at: Option<String>,
    #[serde(default)]
    pub chunking: Map<String, Value>,
    #[serde(flatten)]
    pub extra: Extra,
}

/// What `add_document` reports.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Added {
    pub doc_id: String,
    #[serde(default)]
    pub chunks: u64,
    #[serde(default)]
    pub replaced: u64,
    #[serde(default)]
    pub quality: f64,
    #[serde(default)]
    pub low_quality: bool,
    #[serde(default)]
    pub citations: Vec<String>,
    /// Whether the server kept the Markdown, so `open_document` works.
    #[serde(default)]
    pub kept: bool,
    #[serde(flatten)]
    pub extra: Extra,
}

/// A feed or page the collection is kept current from.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Source {
    pub id: String,
    #[serde(default)]
    pub address: String,
    #[serde(default)]
    pub kind: Option<String>,
    /// `"6h"`, or a number of seconds.
    #[serde(default)]
    pub every: Option<Value>,
    #[serde(flatten)]
    pub extra: Extra,
}

/// What `refresh_sources` reports.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Refreshed {
    #[serde(default)]
    pub added: u64,
    #[serde(default)]
    pub updated: u64,
    #[serde(default)]
    pub unchanged: u64,
    #[serde(default)]
    pub removed: u64,
    #[serde(default)]
    pub failed: Vec<Value>,
    #[serde(flatten)]
    pub extra: Extra,
}

/// Which search a query runs.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Mode {
    /// Dense search over meaning: `text-search`.
    #[default]
    Meaning,
    /// Meaning and keywords fused: `text-hybrid-search`.
    Hybrid,
}

/// Options for [`Client::search`](crate::Client::search).
#[derive(Debug, Clone)]
pub struct SearchOptions {
    pub limit: usize,
    /// The simple filter form: `{"team": "payroll", "price": {"$lt": 100}}`.
    pub filter: Option<Value>,
    pub rerank: bool,
    pub mode: Mode,
}

impl Default for SearchOptions {
    fn default() -> Self {
        SearchOptions {
            limit: 10,
            filter: None,
            rerank: false,
            mode: Mode::Meaning,
        }
    }
}

/// Options for [`Client::create_collection`](crate::Client::create_collection).
#[derive(Debug, Clone)]
pub struct CreateOptions {
    pub dimension: u32,
    pub text_index: bool,
    pub metric: String,
    pub description: Option<String>,
}

impl Default for CreateOptions {
    fn default() -> Self {
        CreateOptions {
            dimension: 384,
            text_index: true,
            metric: "cosine".to_owned(),
            description: None,
        }
    }
}

/// Options for [`Client::add_document`](crate::Client::add_document).
#[derive(Debug, Clone, Default)]
pub struct AddOptions {
    /// The document's id; the filename when left out.
    pub doc_id: Option<String>,
    /// A JSON object put on every chunk.
    pub metadata: Option<Value>,
    /// The chunking strategy, `"markdown"` by default.
    pub chunk: Option<String>,
    pub chunk_size: Option<u32>,
    pub overlap: Option<u32>,
}

/// One point for [`Client::add_texts`](crate::Client::add_texts).
#[derive(Debug, Clone, Default)]
pub struct Text {
    pub id: String,
    pub text: String,
    pub metadata: Option<Map<String, Value>>,
}

impl Text {
    pub fn new(id: impl Into<String>, text: impl Into<String>) -> Text {
        Text {
            id: id.into(),
            text: text.into(),
            metadata: None,
        }
    }

    pub fn metadata(mut self, metadata: Map<String, Value>) -> Text {
        self.metadata = Some(metadata);
        self
    }
}

/// Options for [`Client::add_source`](crate::Client::add_source).
#[derive(Debug, Clone, Default)]
pub struct SourceOptions {
    /// `"feed"` or `"page"`; left out, the server fetches it once to tell.
    pub kind: Option<String>,
    /// How often it is read: `"30m"`, `"6h"`, `"1d"`.
    pub every: Option<String>,
}
