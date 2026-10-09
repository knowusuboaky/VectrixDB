//! Connect with a company sign-in token instead of an API key, and ask who
//! that is.
//!
//!     VECTRIXDB_URL=... VECTRIXDB_TOKEN=... cargo run --example signin_token

use vectrixdb::Client;

#[tokio::main]
async fn main() -> vectrixdb::Result<()> {
    let url = std::env::var("VECTRIXDB_URL").unwrap_or_else(|_| "http://127.0.0.1:8000".into());
    let token = std::env::var("VECTRIXDB_TOKEN").expect("VECTRIXDB_TOKEN");
    let db = Client::new(url).token(token).build()?;

    match db.whoami().await {
        Ok(me) => println!("{}", serde_json::to_string_pretty(&me).unwrap()),
        Err(e) if e.is_auth() => println!("the token was refused: {e}"),
        Err(e) => return Err(e),
    }
    Ok(())
}
