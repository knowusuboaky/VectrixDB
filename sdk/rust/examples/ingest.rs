//! Upload a file into a collection, creating the collection when it is new.
//!
//!     VECTRIXDB_URL=... VECTRIXDB_KEY=... cargo run --example ingest -- docs handbook.md

use vectrixdb::{AddOptions, Client, CreateOptions};

#[tokio::main]
async fn main() -> vectrixdb::Result<()> {
    let mut args = std::env::args().skip(1);
    let collection = args.next().unwrap_or_else(|| "docs".into());
    let path = args.next().expect("a file to upload");

    let url = std::env::var("VECTRIXDB_URL").unwrap_or_else(|_| "http://127.0.0.1:8000".into());
    let key = std::env::var("VECTRIXDB_KEY").unwrap_or_default();
    let db = Client::new(url).key(key).build()?;

    match db.describe(&collection).await {
        Ok(_) => {}
        Err(e) if e.is_not_found() => {
            db.create_collection(&collection, CreateOptions::default())
                .await?;
        }
        Err(e) => return Err(e),
    }

    let bytes = std::fs::read(&path).expect("read the file");
    let filename = std::path::Path::new(&path)
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| "document.txt".into());
    let added = db
        .add_document(&collection, &bytes, &filename, AddOptions::default())
        .await?;
    println!(
        "{}: {} chunks, {} replaced, quality {:.2}",
        added.doc_id, added.chunks, added.replaced, added.quality
    );
    for citation in &added.citations {
        println!("  {citation}");
    }
    Ok(())
}
