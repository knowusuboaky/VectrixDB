# Keep a collection in step with feeds and pages

A news feed, a podcast, a pricing page, a regulator's notices: some of what a collection answers from lives at an address and changes there. `db.sources` is the list of those addresses a collection keeps up with, and a refresh, run on a schedule, reads each one that is due and writes only what changed. A new entry is added, a changed one replaces its last version under the same document id, and one that is the same is left alone, so nothing is indexed twice and every chunk still traces back to the write that stored it.

```bash
pip install "vectrixdb[feeds]"    # RSS 2.0, RSS 1.0 and Atom; JSON Feed and pages need nothing
```

## Add a feed

```python
from vectrixdb import Vectrix

db = Vectrix("news")
db.sources.add("https://news.example.com/feed.xml", every="6h")
report = db.sources.refresh()
print(report)    # 1 source: 20 added, 0 updated, 0 unchanged, 0 removed, 0 failed

hit = db.search("interest rates", limit=1).top
hit.metadata["title"], hit.metadata["link"], hit.metadata["published"], hit.citation
```

The address is fetched once when it is added, to tell a feed from a page; say `kind="feed"` or `kind="page"` and it is not. Nothing is written until the refresh. `every` is how often the source is due: `"30m"`, `"6h"`, `"1d"`, `"1w"`, a number of seconds or a `timedelta`, five minutes at the least, a year at the most, and six hours when it is left out. Adding an address that is already there changes how often it is read and its options, and keeps what it has written.

Each entry is one document. Every chunk of it carries the entry's `title`, `link`, `author`, `published` and `updated`, the feed's title as `feed`, and `source_id`, `source_kind` and `source_address`, so a search can be kept to one source with a filter. The document's id is the entry's link with click tracking (`utm_*`, `fbclid`, `gclid` and the like) and any credential taken out, so one article reached from two feeds is one document, written once. Entries of one feed that share a link, a podcast whose every episode links to the show's home page, are each a document of their own: the one written first keeps the link as its id, and each of the others adds its own entry id after a `#`. An entry with no link is its feed's address and the entry's own id.

RSS 2.0, RSS 1.0 and Atom are read by `feedparser`, from the bytes the guarded fetch brought; it never fetches anything itself. JSON Feed needs nothing. A YouTube channel's feed, `https://www.youtube.com/feeds/videos.xml?channel_id=...`, gives each video's title, description and `video_id`; to index what is said in a video, use `vectrixdb.load_youtube`, or the YouTube routes of [an extraction service](extraction-service.md).

`articles=True` reads the article each entry links to, through the same checks as the feed, and indexes that rather than the summary the feed carries. A site whose robots.txt keeps VectrixDB out of its articles, or that answers with a bot check, gets the feed's own text indexed instead and a note in the report saying so.

## Refresh

```python
db.sources.add("https://www.example.com/pricing", every="1d")   # a page
report = db.sources.refresh()                    # every source that is due
report = db.sources.refresh(force=True)          # every source, due or not
report = db.sources.refresh(only="https://www.example.com/pricing")
report.added, report.updated, report.unchanged, report.failed, report.ok
```

`force` and `only` read a source that is not due yet, but never ask a site sooner than it said: one that asked for time with `Retry-After` is put off still.

A refresh never raises because a source failed. The report says, for each source, what was added, updated, left unchanged, removed or found gone, which entries failed and why, and what was put off. `report.ok` is false when anything failed, `str(report)` is one line for a log, and `report.to_dict()` is the whole of it, for JSON. A failed entry is tried again at the next refresh.

What a refresh costs:

- **A feed or a page that has not changed costs one request.** It is asked with the `ETag` and `Last-Modified` it gave last time, and a `304` writes nothing. They are kept only once every entry of the answer is written: while entries wait for the next refresh, or one failed, the next refresh asks for the whole feed again, so a `304` never hides what is still to be written.
- **An entry that has not changed is not read again.** Its title, link, dates and text are compared with what was written last time, without fetching its article or its episode.
- **One refresh writes fifty entries a source at the most.** The rest are reported as waiting and are written by the next one, so a feed's first refresh after a long backlog, or a podcast with a hundred episodes to transcribe, is spread over several. `refresh(max_items=...)` says otherwise. Only a feed's first five hundred entries are read.
- **An entry that drops out of a feed keeps its documents.** A feed is the latest few, and an article that scrolled off it is still the article.

`db.sources.list()` shows each source with when it was last read, how that went and when it is next due. `db.sources.remove(address)` stops keeping up with one and leaves its documents, unless `delete_documents=True`, which takes every document it wrote that no other source of the collection wrote too. `db.sources.reset()` forgets what was written, so the next refresh writes everything again, for a collection cut again in a new way.

`db.clear()` empties the collection and keeps its sources, so the next refresh fills it again. Deleting the collection forgets them.

## Add a page

A page is one document: its main text, read the way `add_document` reads an HTML file, so the menus, banners and footer around it are left out and a change to them is not a change. A PDF, a Word file or a Markdown file at an address is read as it would be from disk.

The page is asked with the `ETag` and date it gave last time, and written again only when its text changed. A page that answers `404` or `410` after it was indexed is reported gone and its chunks are kept, since a page that is down for an afternoon has not been withdrawn. `delete_when_gone=True` removes them instead:

```python
db.sources.add("https://www.example.com/rates", every="1h", delete_when_gone=True)
```

## Follow a podcast

A podcast is a feed whose entries carry their episode's sound. When the collection has an audio engine registered for the episodes' files, each new episode is downloaded and transcribed, and a hit in it is cited to the second, as any recording is. Without one, or with `transcribe=False`, an episode is its show notes. Nothing is downloaded to find out which.

```python
from vectrixdb import Vectrix
from vectrixdb.extract.engines import Whisper

pods = Vectrix("pods", extractors={".mp3": Whisper("base"), ".m4a": Whisper("base")})
pods.sources.add("https://pod.example.com/feed.xml", every="1d")
pods.sources.refresh(max_items=5)   # five episodes a refresh: each is a download and a transcription
```

An episode may be up to 300 MB and take up to ten minutes to arrive; `Feed(address, max_audio_bytes=...)` sets another size. An episode's address is often a signed link on a CDN, with a token in it. The episode is fetched with the token, and the `audio` the chunks carry is the address without it.

## Schedule it

Refreshing is what a scheduler does, and VectrixDB runs no timer of its own: a timer inside a server runs once for every copy of the server, stops when it is redeployed and says nothing when it fails, and the scheduler you already have does none of that. Every refresh takes a lease on each source while it reads it, so two that overlap, a slow one and the next, cron on two machines, a CronJob and somebody's manual run, never read one source at once: the second passes over it as busy. A refresh that dies lets go when its lease runs out, thirty minutes on, and one still at work keeps its lease for as long as it needs.

### With cron, where the data is

```bash
# Every fifteen minutes; each source is read when it is due.
*/15 * * * * vectrixdb sources refresh --path /srv/vectrixdb >> /var/log/vectrixdb-sources.log 2>&1
```

`vectrixdb sources refresh` with no `--name` refreshes every collection at that path that keeps up with a source. It prints a line for each source and exits 1 when anything failed, so the scheduler's own alerting sees it. The command line opens collections on this machine's disk, as `vectrixdb ingest` does. The rest of it:

```bash
vectrixdb sources add https://news.example.com/feed.xml --name news --every 1h
vectrixdb sources add 'https://wire.example.com/feed?key=${WIRE_KEY}' --name news --kind feed
vectrixdb sources list --name news
vectrixdb sources remove https://news.example.com/feed.xml --name news --delete-documents
vectrixdb sources refresh --name news --force --max-items 20
```

### A server: a Kubernetes CronJob, an Azure Functions timer

A server whose index lives in a storage backend, Azure AI Search for instance, is refreshed through its own route by anything that can send a POST. Give the scheduler a key of its own, with the operator role and only the collections it refreshes:

```bash
vectrixdb keys add source-refresh --role operator --collection news
```

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: vectrixdb-sources
spec:
  schedule: "*/15 * * * *"
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      backoffLimit: 0
      template:
        spec:
          restartPolicy: Never
          containers:
            - name: refresh
              image: curlimages/curl:8.10.1
              env:
                - name: VECTRIXDB_KEY
                  valueFrom:
                    secretKeyRef: {name: vectrixdb-source-refresh, key: api-key}
              args:
                - --fail-with-body
                - --silent
                - --show-error
                - --max-time
                - "900"
                - -X
                - POST
                - -H
                - "api-key: $(VECTRIXDB_KEY)"
                - http://vectrixdb.vectrixdb.svc:7337/api/v1/collections/news/sources/refresh
```

```python
# function_app.py
import json
import os
import urllib.request

import azure.functions as func

app = func.FunctionApp()


@app.timer_trigger(schedule="0 */15 * * * *", arg_name="timer")
def refresh_sources(timer: func.TimerRequest) -> None:
    server = os.environ.get("VECTRIXDB_URL", "https://vectors.your-company.example")
    request = urllib.request.Request(
        f"{server}/api/v1/collections/news/sources/refresh",
        method="POST",
        headers={"api-key": os.environ["VECTRIXDB_KEY"]},
    )
    with urllib.request.urlopen(request, timeout=600) as reply:
        report = json.load(reply)
    if not report["ok"]:
        # A failed run is what Application Insights alerts on.
        raise RuntimeError(f"sources failed: {report['failed']}")
```

The reply is the refresh's report, with `ok` false when a source or an entry failed. App Service ends any request at 230 seconds: a collection with a lot to write each time is better refreshed more often with a smaller `max_items`, since each call writes no more than that for one source.

## Over REST

| Route | Who may | |
|---|---|---|
| `GET /api/v1/collections/{name}/sources` | whoever may see where its chunks came from: a viewer, a reader key and up | the sources as kept |
| `POST /api/v1/collections/{name}/sources` | an operator or an admin | `{"address": "...", "every": "6h", "kind": "feed", "articles": false}` |
| `DELETE /api/v1/collections/{name}/sources/{id}` | an operator or an admin | `?delete_documents=true` takes its documents too |
| `POST /api/v1/collections/{name}/sources/refresh` | an operator or an admin | `{"force": false, "source": null, "max_items": 50}` |

With sign-in on, every call is in the access log, with who made it. An address that reads `${NAME}` from the environment is refused over the API with a `400`: a request could otherwise have the server send its own settings, its keys among them, to whatever host the request names. Add those from Python or the command line on the server. A collection that carries an entitlement policy is refused, as the document routes refuse it.

## Write your own Source for a licensed feed

A news wire, a market data feed, a records system: anything that is not an address to fetch is a `Source` of your own. Give it a `kind`, yield an `Item` for each document in `read()`, and register it. `key` is the item's id within the source, `fingerprint` anything that changes when the item does, a revision number or an updated date, and `read` a function called only when the item is new or changed, so a story that costs a request is not fetched again for nothing.

```python
from vectrixdb import Vectrix
from vectrixdb.sources import Item, Source, register_source

# A stand-in for the wire's client: stories by desk, each with a revision.
STORIES = {
    "energy": [
        {
            "id": "w-101",
            "revision": 3,
            "headline": "Oil steady as talks resume",
            "url": "https://wire.example.com/w-101",
            "time": "2026-10-07T06:00:00Z",
            "body": "Oil prices held steady in early trading as supply talks resumed.",
        }
    ]
}


class Wire(Source):
    """One desk of a licensed news wire."""

    kind = "wire"

    def read(self, context):
        for story in STORIES.get(self.address, []):
            yield Item(
                key=story["id"],
                fingerprint=str(story["revision"]),
                title=story["headline"],
                link=story["url"],
                published=story["time"],
                read=lambda story=story: story["body"],
            )


register_source("wire", Wire)
wire = Vectrix("wire")
wire.sources.add(Wire("energy"), every="1h")
report = wire.sources.refresh()
str(report), wire.search("oil prices", limit=1).top.metadata["title"]
```

A source is kept as its kind, its address and its `settings()`, and built again from them by every refresh, in whatever process runs it. So keep secrets out of both: read the wire's key from the environment inside `read()`, or write `${NAME}` in the address and read `context.address`, which has it filled in, where `self.address` keeps the name. A refresh in another process finds the kind through an entry point in your package:

```toml
[project.entry-points."vectrixdb.sources"]
wire = "acme_wire.source:Wire"
```

A source that does fetch addresses fetches them with `context.get(url, headers)`, which goes through every check below. `context.read_bytes(data, name, source=...)` reads what came back the way the collection reads a file of that name, its own extractors included.

## What is refused, and why

Every fetch, of a feed, a page, an article or an episode, goes through one guard. An address that is refused when it is added is refused before anything is kept.

| What | What happens | Why |
|---|---|---|
| An address that is not `http` or `https` | refused | a feed has no business reading `file://` or `ftp://` |
| A name and password before the host, or a token, key, signature or code in the address, its query, a path parameter such as `;jsessionid=` or a `#/` route | refused when it is added: write `${NAME}` in its place | the address is kept where every operator can list it, and written on every chunk |
| A host that resolves to a private, loopback, link-local, carrier-grade NAT, multicast, reserved or documentation address, IPv6's unique-local and site-local ranges, IPv4 written as IPv6, or Azure's platform address `168.63.129.16` | refused when it is added and at every fetch, before anything is sent | the cloud's metadata service at `169.254.169.254`, an admin page on localhost and a database on the private network are all addresses |
| A cloud's metadata service: `169.254.169.254`, Alibaba Cloud's `100.100.100.200`, AWS's `fd00:ec2::254` | refused always, for a host named in `VECTRIXDB_SOURCES_INTERNAL_HOSTS` too | it hands out the machine's own credentials, and no feed lives there |
| A name that resolves somewhere else the second time | the connection goes to the address that was checked | DNS rebinding |
| A redirect | checked like the first address; from `https` to `http` refused; five at the most; to another site, only `Accept` and `User-Agent` go with it, never a key or a cookie a source sent | a public site that redirects inward, or hands the request on to someone else |
| More than 20 MB, an episode more than 300 MB, or longer than 30 seconds in all, an episode ten minutes, however slowly the server sends it | refused at the cap | a feed that never ends, or one that arrives a byte at a time |
| A path robots.txt keeps VectrixDB from | the source fails, naming robots.txt | the site said no |
| A robots.txt that cannot be read | the source is put off to the next refresh | RFC 9309 says to take that as no, for now |
| `429` or `503` | the source is put off until the time the site asked for, a week at the most, or the next refresh | the site asked for time |
| A bot check: Cloudflare, DataDome, HUMAN (PerimeterX), Imperva, Akamai, AWS WAF, a page that only asks to enable JavaScript and cookies | refused, naming the site and what it sent | it is not the page, and indexed it would put "Just a moment..." in answers |

Requests to one host are a second apart, or the robots.txt `Crawl-delay` apart when that is longer, a week at the most, and a wait of more than a minute is left to the next refresh. A process keeps one pace, one copy of each site's robots.txt and each site's request for time across all its refreshes, so refreshes that follow each other closely, or run side by side, ask a site no more often than one would. Every request says who it is, `VectrixDB/<version> (+https://github.com/knowusuboaky/VectrixDB)`, so a site's owner can find out what it is and allow it or not. VectrixDB does not get past a bot check, and does not pretend to be a browser: for a site that sends one, ask for a feed or an API, or, if the site is yours, let VectrixDB's requests through.

A secret an address needs goes in it as `${NAME}`: `https://wire.example.com/feed?key=${WIRE_KEY}`. The value is read from the environment each time the source is fetched, sent, and never kept, listed, logged, or written on a chunk, a citation or a page's file name, and a message or a redirect that would repeat it has it taken back out.

## Where sources are kept

Sources, what each has written and their leases are records beside the collection records: in the collection store when the database has one, `VECTRIXDB_COLLECTION_STORE` in Cosmos DB, PostgreSQL or DynamoDB, so every server sees the same list and the same leases; in the database's own `_vectrixdb.db` otherwise. A deployment on Azure AI Search that keeps its collection settings in Cosmos DB keeps its sources there too, and the documents go to the search index like any other. See [Choose a storage backend](storage-backends.md) and [the settings](../reference/settings.md).

## Settings

| Setting | |
|---|---|
| `VECTRIXDB_SOURCES_HOSTS` | the only hosts a source may fetch from, redirects included: `news.example.com,*.example.org`. Unset, any public host |
| `VECTRIXDB_SOURCES_INTERNAL_HOSTS` | intranet hosts a source may fetch from although they resolve to private addresses. Link-local addresses, where metadata services answer, stay refused for them too |
| `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` | read as every other tool reads them. The address is still resolved and checked here before the proxy is asked, so a host this server cannot look up is refused. A plain `http` address is refused through a proxy, since the proxy looks its name up again and plain `http` cannot tell where that reached: use the `https` address, or name the host in `NO_PROXY`. What the proxy connects to is the proxy's decision, so keep its own rules against internal addresses |
| `VECTRIXDB_OFFLINE` | nothing is fetched and no name is looked up: every source is put off, and one is added without its address being read, so say its `kind` |
