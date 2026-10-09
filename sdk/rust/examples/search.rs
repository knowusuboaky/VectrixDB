//! Search a collection and print each hit with its citation.
//!
//!     VECTRIXDB_URL=http://127.0.0.1:8000 VECTRIXDB_KEY=... cargo run --example search -- handbook "how long do refunds take?"

use vectrixdb::{Client, SearchOptions};

#[tokio::main]
async fn main() -> vectrixdb::Result<()> {
    let mut args = std::env::args().skip(1);
    let collection = args.next().unwrap_or_else(|| "handbook".into());
    let words: Vec<String> = args.collect();
    let query = if words.is_empty() {
        "how long do refunds take?".to_string()
    } else {
        words.join(" ")
    };

    let url = std::env::var("VECTRIXDB_URL").unwrap_or_else(|_| "http://127.0.0.1:8000".into());
    let key = std::env::var("VECTRIXDB_KEY").unwrap_or_default();
    let db = Client::new(url).key(key).build()?;

    let options = SearchOptions {
        limit: 3,
        ..Default::default()
    };
    for hit in db.search(&collection, &query, options).await? {
        let text = hit.text.split_whitespace().collect::<Vec<_>>().join(" ");
        println!("{:.3}  {}\n       {}", hit.score, hit.citation, text);
    }
    Ok(())
}
