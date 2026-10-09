//! A synchronous client, behind the `blocking` feature.

use serde_json::Value;

use crate::error::{Error, Result};
use crate::types::{
    AddOptions, Added, Collection, CreateOptions, Document, Hit, Refreshed, SearchOptions, Source,
    SourceOptions, Text,
};
use crate::Client;

/// The same calls as [`Client`], each one blocking until it is answered.
///
/// It drives the async client on a runtime of its own, so it must not be
/// used from inside another async runtime.
pub struct BlockingClient {
    inner: Client,
    runtime: tokio::runtime::Runtime,
}

impl std::fmt::Debug for BlockingClient {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("BlockingClient")
            .field("url", &self.inner.url())
            .finish()
    }
}

impl BlockingClient {
    pub(crate) fn from_client(inner: Client) -> Result<BlockingClient> {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|e| Error::Transport(format!("could not start a runtime: {e}")))?;
        Ok(BlockingClient { inner, runtime })
    }

    /// The async client underneath.
    pub fn inner(&self) -> &Client {
        &self.inner
    }

    pub fn health(&self) -> Result<bool> {
        self.runtime.block_on(self.inner.health())
    }

    pub fn ready(&self) -> Result<bool> {
        self.runtime.block_on(self.inner.ready())
    }

    pub fn whoami(&self) -> Result<Value> {
        self.runtime.block_on(self.inner.whoami())
    }

    pub fn collections(&self) -> Result<Vec<Collection>> {
        self.runtime.block_on(self.inner.collections())
    }

    pub fn describe(&self, name: &str) -> Result<Collection> {
        self.runtime.block_on(self.inner.describe(name))
    }

    pub fn create_collection(&self, name: &str, options: CreateOptions) -> Result<Collection> {
        self.runtime
            .block_on(self.inner.create_collection(name, options))
    }

    pub fn delete_collection(&self, name: &str) -> Result<()> {
        self.runtime.block_on(self.inner.delete_collection(name))
    }

    pub fn add_document(
        &self,
        collection: &str,
        bytes: impl AsRef<[u8]>,
        filename: &str,
        options: AddOptions,
    ) -> Result<Added> {
        self.runtime.block_on(
            self.inner
                .add_document(collection, bytes, filename, options),
        )
    }

    pub fn add_texts(&self, collection: &str, texts: Vec<Text>) -> Result<u64> {
        self.runtime
            .block_on(self.inner.add_texts(collection, texts))
    }

    pub fn search(
        &self,
        collection: &str,
        query: &str,
        options: SearchOptions,
    ) -> Result<Vec<Hit>> {
        self.runtime
            .block_on(self.inner.search(collection, query, options))
    }

    pub fn documents(&self, collection: &str) -> Result<Vec<Document>> {
        self.runtime.block_on(self.inner.documents(collection))
    }

    pub fn open_document(&self, collection: &str, doc_id: &str) -> Result<String> {
        self.runtime
            .block_on(self.inner.open_document(collection, doc_id))
    }

    pub fn delete_document(&self, collection: &str, doc_id: &str) -> Result<u64> {
        self.runtime
            .block_on(self.inner.delete_document(collection, doc_id))
    }

    pub fn sources(&self, collection: &str) -> Result<Vec<Source>> {
        self.runtime.block_on(self.inner.sources(collection))
    }

    pub fn add_source(
        &self,
        collection: &str,
        address: &str,
        options: SourceOptions,
    ) -> Result<Source> {
        self.runtime
            .block_on(self.inner.add_source(collection, address, options))
    }

    pub fn refresh_sources(&self, collection: &str) -> Result<Refreshed> {
        self.runtime
            .block_on(self.inner.refresh_sources(collection))
    }

    pub fn delete_source(
        &self,
        collection: &str,
        source_id: &str,
        delete_documents: bool,
    ) -> Result<()> {
        self.runtime.block_on(
            self.inner
                .delete_source(collection, source_id, delete_documents),
        )
    }
}
