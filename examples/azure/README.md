# VectrixDB on Azure, one step at a time

A PDF lands in a blob container. Nobody presses anything. A minute later it is
in Azure AI Search, cut into chunks, its pictures described, its scanned pages
read, and a search of the dashboard finds it by page.

The app that does it also serves the library's own API, about sixty routes,
so the same deployment that ingests is the one other people search.

This folder is how you see that happen for yourself, on your own account, with
your own files. Every step is a numbered script, each one does a single thing,
and `99_delete_everything.py` removes all of it when you are done.

Nothing here is seeded. Every number on the dashboard at the end came from your
documents going through the real pipeline.

## What it costs

Roughly **10 to 25 CAD** for a few days, and almost all of it is one resource.

| | Cost |
| --- | --- |
| Azure AI Search, basic tier | about 3 to 4 CAD a day **while it exists** |
| Azure AI Search, free tier | 0, but 50 MB and no semantic ranker |
| Functions, Flex Consumption | inside the free grant at this volume |
| Blob storage | pennies |
| Cosmos DB, free tier | 0 for 1000 RU/s and 25 GB: its one database, `data_db`, has exactly that |
| Document Intelligence, free tier | 0 for 500 pages a month |
| Speech, free tier | 0 for 5 hours a month |
| Azure OpenAI | cents: a fraction of a cent per picture |

The search service is billed by the hour whether anybody searches it or not.
That is the only thing that runs up a bill while you sleep, and
`99_delete_everything.py` is what stops it.

**Set a budget before you start.** `00b_set_budget.py` does it in one command:
25 CAD a month on the whole subscription, with an email at half of it, at four
fifths, at all of it, and one more when Azure *forecasts* you will pass it by
month end. The forecast alert is the useful one, because it fires while there
is still credit left to save.

A budget tells you and stops nothing: Azure does not switch a service off when
you pass one. It covers the whole subscription rather than this walkthrough's
resource group, on purpose, because a resource made by hand outside the group
is exactly the kind that gets forgotten.

```bash
python 00b_set_budget.py --show      # the budget, and what has been spent
python 00b_set_budget.py --amount 50 # change it; one budget, replaced
python 00b_set_budget.py --remove    # take it off
```

## Before you spend anything

Most of this can be proved on your own machine for nothing, and doing that
first is how you find a problem while it is still free:

```bash
pip install "vectrixdb[azure,documents,ocr-azure,ffmpeg]" pillow
python -c "from vectrixdb import Vectrix; db = Vectrix('try'); print(db.add_document('.local/blob/data_db/ingestion/raw/financial/td/td-annual-report-2025.pdf'))"
vectrixdb serve
```

That reads a real PDF, cuts it, indexes it and shows it to you, with no Azure
account involved. If a file reads badly here it will read badly there.

## The steps

```bash
cp settings.example.env settings.env      # then change VX_PREFIX
pip install -r requirements.txt           # what this machine needs to run the steps
python 00_login.py                        # sign in, check the names are free
python 00b_set_budget.py                  # 25 CAD a month, alerts by email
python 01_create_resources.py             # providers, storage, three containers, the queue
python 02_create_search.py                # the search service
python 03_create_ai_services.py           # document intelligence, speech, vision, language, translator, openai, cosmos
python 04_push_local_to_blob_cosmosdb.py  # fetch what .local is missing; the records, then the first drop, go up
python 05_create_extraction_function_app.py   # the extraction service, a utility API on its own
python 06_create_main_function_app.py         # the main app, twice: an ingest app that reads every file, a query app that answers; the event link
python 06_create_main_function_app.py --add-person you@example.com   # you, the first admin: a one-time link, printed here
python "07_push_new_files_from local_to_blob_cosmosdb.py"   # the trigger test: walk away, it still reads

python 10_retrieve_finds_the_most.py      # the three retrievals, each through one of the run's picks
python 11_retrieve_best_for_balance.py    #   against a dashboard on this machine; see below
python 12_retrieve_best_for_time.py

python 99_delete_everything.py            # when you stop for the day
```

## The three retrievals

The run names three setups and does not choose between them. These three
scripts ask the same questions through each, so you can read the difference
rather than take the run's word for it.

| | What it is |
| --- | --- |
| `finds_the_most` | found the answer for the most questions, whatever it cost |
| `best_for_balance` | the fastest within four points of that |
| `best_for_time` | the fastest within eight points of that |

Each signs in with a key named after its pick, so the Access page ends with
three rows under "who reads most", one a setup, and the Overview's searches
chart has something in it. That is the comparison on one card instead of in
three terminals.

```bash
python 10_retrieve_finds_the_most.py      # or 11_retrieve_best_for_balance.py, 12_retrieve_best_for_time.py
python 10_retrieve_finds_the_most.py --port 8000 --collection financial --question "What was the total revenue?"
```

They go through the dashboard's Backend on this machine, `http://localhost:8000`
unless `--port` says otherwise, so each search lands in the access log the
Access and Overview pages are drawn from, at `/#/access` and `/#/overview`.
Minting a setup's key is an admin's act, so the scripts need a key that may:
`VX_API_KEY` in `settings.env`, the same key the dashboard's Backend holds as
`UPSTREAM_KEY`, or failing that the query app's own `VECTRIXDB_API_KEY`, read
back from Azure. With neither the script stops and says so. `--dry-run` says
what it would ask and asks nothing.

One gap worth knowing: a setup names the engine, the method, the reranker and
which vectors answer. The REST routes carry the first three, not the last, so
on an index built with two vectors the script says so rather than quietly
searching something else.

Every resource is asked for in `VX_LOCATION` first. When that region will not
take it, for no room or no quota, it is asked for in each region of
`VX_ELSEWHERE`, in the order written, and the script says which one took it.
Azure OpenAI is asked what quota is left before its account is made, so it
goes where its models can actually be deployed.

Every script takes `--dry-run`, which prints each `az` command that would change
something and runs none of them. The two pushes, 04 and 07, look first,
reading only, so a dry run of either says what the container and Cosmos DB
already hold and plans only the rest. Every script is safe to run twice: each one checks what is already there.
Every `az` command is printed before it runs, so you can read what is about to
happen, or type it yourself.

## The extraction service, on its own

`05_create_extraction_function_app.py` makes a second Function App,
`<prefix>-extract`, that stands alone: a backend for any work that needs text
out of something. Files, addresses and YouTube videos go in, Markdown or JSON
comes out, and nothing is indexed. It is the library's extraction service as
it ships, and every route is in
[Run an extraction service](../../docs/how-to/extraction-service.md).

It is not part of the ingest app and needs nothing of it: no search service,
no ingest queue, no Event Grid link. It needs the group and the storage
account from 01, and uses Document Intelligence, Speech, Vision and Language
from 03 when they are there. Without Document Intelligence, pictures answer
503; without Speech, recordings, videos and YouTube do; without Vision, a
picture is read for its words and not described; without Language, a document
is masked by the patterns alone. 03 makes a Translator too, on its free tier,
and the `/translate` routes answer with it; with `VX_TRANSLATOR=no`,
`/translate/text` and `/translate/detect` answer 503 and name the setting
they need.

Every file is masked as it is read. The main app asks for it on every call,
`VECTRIXDB_EXTRACTOR_MASK=1`, and the extraction app takes the identifiers
out before a word comes back: emails, phone numbers, cards, social and bank
numbers, keys and passwords by their shape, and names and addresses through
Azure AI Language when 03 made it. So the Markdown the main app keeps and the
index it builds never held them, the raw file stays as uploaded, and the
setup spinner's Masking stage says what went. Nothing is set per collection.

- **Its key** is made on the first run and kept in the app's settings, and
  nowhere else. A second run keeps it, so its callers are not cut off. Read it
  when you need it:
  `az functionapp config appsettings list --name <prefix>-extract --resource-group <prefix>-rg --query "[?name=='VECTRIXDB_API_KEY'].value" --output tsv`
- **Its paths** are yours to choose in `settings.env`: `VX_EXTRACT_PREFIX` for
  the path every route lives under, and `VX_EXTRACT_GATEWAY_PATHS` for a
  gateway that gives each endpoint a path of its own. The script checks both
  before it sends them, because a list the app cannot read stops it loading.
- **The addresses it may fetch** are `VX_EXTRACT_URL_HOSTS`. With none, the
  routes that fetch an address refuse every one, which is the safe way round.
  The example settings name `www.td.com`, where the walkthrough's own reports
  come from, and nothing else.
- **A long file** is read by the main app in pieces, each one call to this
  app inside Azure's 230 seconds: sound in pieces of about ten minutes, cut
  at a pause, a video's sound the same way, a PDF twenty pages at a time.
  The answers go back together with their times and page numbers moved on.
  `EXTRACTION_BATCH_MINUTES` and `EXTRACTION_BATCH_PAGES` change the sizes.
- **A long job**, a saved YouTube transcript, goes through the container
  `extraction` and the queue `extract-jobs` in the same storage account. The
  app makes both itself.
- **The wheel** is looked inside before it is sent. One built before the
  extraction service had what the app imports would install and then load
  nothing, so the script says to rebuild it instead.

`99_delete_everything.py` deletes it with the rest of the group.

### Extraction app: what to check

Two addresses and one message. `GET <app>/health` needs no key and proves the
worker indexed the code; `GET <app>/health/wiring`, also without a key, says
what the app was given without opening anything: which readers it has, by
service, and what it does without each; the masking engine that loaded and
the languages it covers; the jobs backend; the hosts an address route may
fetch from; the upload limit; and how callers get in. It carries no key and
no endpoint, so it is safe to paste into a bug report. When the app answers
404 to both, its settings did not hold: the live log, under the function app
in the portal, has one message that lists every setting that is wrong or
missing, `VECTRIXDB_EXTRACT_JOBS=azure` with no storage connection, a host in
`VECTRIXDB_EXTRACT_URL_HOSTS` that is not a host, a masking engine the library
does not know, rather than the first of them raising mid-stack.

## What is being built

```
   your files                                    what you see
       |                                              |
  the two pushes                                the dashboard
       |                                              |
       v                                              |
  ingestion/raw/                                      |
       |                                              |
       |  a blob is written                           |
       v                                              |
   Event Grid  ──writes a message──>  ingest queue    |
                                            |         |
                                            v         |
                                    the main app      |
                                            |         |
              sends it, by its suffix, to the         |
              extraction app, which reads it: pypdf,  |
              Document Intelligence, Speech, Vision   |
                                            |         |
                                            v         |
                            ingestion/markdown/       |
                             the Markdown as read     |
                                            |         |
                                            v         |
                            ingestion/chunks/         |
                             a JSON line a chunk      |
                                            |         |
                                            v         |
                                   Azure AI Search ───+
                                    the index of record
```

The main app is one folder deployed twice. The **ingest app** runs the
queue trigger and answers `/health`; the **query app** serves the library's
API and the dashboard and never reads a file, its trigger switched off by an
app setting. Both read the same stores, so a document is searchable the
moment its chunks land, and they scale apart, the one on the queue's length
and the other on requests: a burst of two hundred files runs on machines
that are not answering anybody's question. `INGEST_ROLE` tells each which
it is; one app doing both is still the default for a laptop.

The queue in the middle is the part worth understanding. Event Grid could call
the function directly, but then a two hundred page report would be cut off
after a few minutes and nobody would be told. Writing a message takes
milliseconds and never fails for being slow; the function then has an hour for
one file. A read that fails goes round again, and after five tries the message
sits in `ingest-poison`, which is your list of files that would not read.
The dashboard's Ingest page lists that queue in words: which file, what
stopped it and why, how many tries, when, with **Retry**, which puts the
message back, and **Drop**, which deletes the file and everything of it.
The Overview's Needs attention card counts them, so a file that will never
read is seen the day it fails.

Three more things keep a bad hour from filling that queue. A file the
extraction app could not be reached for, or answered 429 or 503 to, is asked
again inside the try, waiting longer each time with jitter and honouring
Retry-After, before the queue ever sees a failure (`VECTRIXDB_EXTRACTOR_RETRIES`,
three by default). When five reads in a row could not reach the app at all,
ingestion pauses: messages go back on the queue with a delay instead of
burning their tries, one probe every five minutes goes through, and the
Ingest page and Needs attention say "paused since, next try at"
(`INGEST_BREAKER_FAILURES`, `INGEST_BREAKER_SECONDS`). And a file saved again
with the same bytes is recognised by its hash under its new ETag and not read
again, so a re-drop costs a fetch and nothing more. `INGEST_CHUNKS_PER_SECOND`
puts a pace on index writes for a burst of files.

Each file goes through three steps, and each step's output is written down
before the next begins. The Markdown is kept in `ingestion/markdown/`. It is
read back from there and cut into chunks, which are written to
`ingestion/chunks/` as `<file>.jsonl`, a JSON line a chunk. Only then are they
embedded into the index. A step that fails leaves what came before it, and the
next try starts from the Markdown rather than reading the file again. Delete a
file from `ingestion/raw/` and its Markdown, its chunks and its place in the
index go with it; no copy is kept. A collection made for graph search keeps
its knowledge graph in the same container, `ingestion/graph/<collection>/graph.json`,
one object a collection, written when the dashboard's Graph tab extracts it
and read by every instance; it records the build it was read from, so the
tab says when the index has moved on and offers to extract again.

## Three collections

One index each in the search service, one folder each inside `ingestion/raw/`,
and the same folder again inside `ingestion/markdown/` and `ingestion/chunks/`
for what the function wrote. A blob at `ingestion/raw/financial/td/ar2025.pdf` goes to the financial
collection as the document `td/ar2025.pdf`; a blob whose folder names no
collection is left alone rather than guessed at, because a guess is a document
in the wrong index that nobody notices until they search the right one.

| Collection | What lands in it | Who may retrieve from it |
| --- | --- | --- |
| `financial` | the two TD reports | one person, by email |
| `media` | the WAV and the two videos | one person, by email, until single sign-on is set up |
| `misc` | the two PNGs | everyone at one domain |

Who may retrieve from a collection is its record's to say, one file each in
`.local/cosmosdb/data_db/collection_records/`, and it is checked on the server
before anything is searched: somebody the record does not name gets the reply
a collection that is not there gets, and an admin is told why. A collection
with no policy answers nobody, which is why New collection on the dashboard
asks for one before a single file is read.

The seeds show two of the three ways. The third, a security group read from
the sign-in token, needs single sign-on, since nothing else carries groups, so
`media` names its people until single sign-on is set up. Then its record
becomes a group, narrowed to the people on its list, and 07 sends it:

```json
{"method": "token", "allow": [{"id": "<the group's object id>", "name": "Media team"}], "people": [{"email": "you@company.com"}]}
```

To see a refusal for yourself, open a collection's Settings and use Check
someone with an address the record does not list.

One index you may see beside those three is `vectrix-collections`, empty, 0
documents and 0 bytes. It is the library's catalog index under its default
prefix, left by a run from before 06 set `AZURE_SEARCH_INDEX_PREFIX` empty;
the catalog the app reads is plain `collections`. `06_create_main_function_app.py`
deletes it on its next run when it is empty, and leaves it alone with a word
when it holds anything, and `99_delete_everything.py` lists every index with
its document count so a leftover is visible before the group goes.

### Where every decision is recorded

In the storage account's third container, `audit`, which keeps what it
holds: every sign-in, read and refusal is a line in `audit/access/`, one
append blob a UTC day, and `audit/decisions/2026/09/23.jsonl` takes a line for
each decision an entitlement policy makes, which stays empty unless a record
carries one. The container carries a
retention policy of `VX_AUDIT_DAYS` days, 7 unless you say, that lets a line
be added and never changed, by the app or by you, and the library will not
write to a container without one. The dashboard's Audit and Access pages
read both back, whichever instance wrote them.

The policy is left unlocked, so `99_delete_everything.py` can take it off and
remove everything. Locking it is what a regulator asks for, and it cannot be
undone: see section 4 of `01_create_resources.py` before you do.

### Who may retrieve from a collection

Each collection's record says, one file a collection in the mirror:

    .local/cosmosdb/data_db/collection_records/financial.json
    .local/cosmosdb/data_db/collection_records/media.json
    .local/cosmosdb/data_db/collection_records/misc.json

```json
{
  "name": "financial",
  "path": "raw/financial/",
  "policy": {"method": "store", "allow": [{"email": "you@example.com"}]}
}
```

The policy is one of two, and the method says where it reads from:

| `method` | Who may retrieve | `allow` |
| --- | --- | --- |
| `token` | anyone in one of the security groups, matched by object id against the groups Entra signed into their sign-in token | `{"id": <the group's object id>, "name": ...}`, one a group |
| `store` | the people on a list kept with the collection: specific people by email, as long a list as you like, and a domain for everyone at it, matched against the address they signed in with | `{"email": "ama@company.com"}` or `{"domain": "company.com"}`, one a row |

Nobody types anything into a request: the server reads the token, or the
address the person signed in with, and matches it against the record. On
the dashboard the two are cards, the sign-in token and the membership store;
pick one and its list appears under it, and a pasted batch of addresses
becomes rows. `media.json` names a placeholder group id to replace with a
real one.

The check runs before anything is searched, on every route that reads a
collection, the function app's `/search/answer` included. Everyone who
reaches the server sees that a collection exists, its name and its size;
somebody the policy does not name is refused when they search it, told why
with a 403 and its code, and the refusal is logged. A collection with no
policy, or no record, answers nobody. `POST /api/v1/access/check` tries an
address, and the groups to try, and says whether they would get in and why,
without searching anything: that is how a policy is tested before anybody
signs in.

Steps 04 and 07 push each record into Cosmos DB, the `data_db` database's
`collection_records` container, as the item `collection.financial`. Every
server reads it from there, the function app and the hosted API alike, so
the two can never enforce different rules. Change a policy in its file and
run step 07: it is in force everywhere within thirty seconds, with nothing
published again. A collection with no file gets one written with no policy
the first time a push runs, and answers nobody until the file names someone.

The policy is the whole rule: there is no separate visibility, and
identifiers in what people are shown are always masked. The dashboard's
Policy tab edits the same record a push writes. Deleting a collection takes
its record with it, so one made again under the name answers nobody until it
is given a policy.

## What each file is for

| File | What it exercises |
| --- | --- |
| `td-annual-report-2025.pdf` | a long document: pages, tables, figures, and the vision model on real charts |
| `office.png`, `example.png` | Document Intelligence on a photograph |
| `male.wav` | Speech, cited by the minute |
| `toddler.mp4` | ffmpeg takes the sound out, then Speech. Only the sound is read, never the picture |
| `video.mp4` | dropped later, to watch the trigger fire with nobody watching |
| `ar2025-consolidated-financial-statements.pdf` | a second build, and text the index nearly has already |

## Filling the dashboard

Several pages draw nothing from a bare index, and that is correct rather than
broken. To see all of them:

| Page | What it needs |
| --- | --- |
| Overview, and its charts | sign-in on, then search a few times |
| a collection's Builds chart | two batches, so there are two builds |
| Ingest quality bars | two or more files in one run |
| Evaluate, the Retrieval tab | a run: `POST /api/v1/evaluations/run` |
| Evaluate, the Chunking tab | a run: `POST /api/v1/chunking/run` |
| Audit | the financial collection, which has one, being searched |
| Access, who reads most | sign-in on, and more than one caller |

One honest limit: the Overview's charts cover fourteen days and a collection's
growth thirty, and a deployment made this morning has everything on one day.
They will show one bar. That is the truth about a new deployment, not a fault.

## Things that will go wrong, and what they mean

**A name is taken.** `00_login.py` checks the two global names first. Change
`VX_PREFIX` in `settings.env` and run it again.

**Azure OpenAI is refused.** Some subscriptions have to ask for access. Set
`VX_OPENAI=no` and everything else works; pictures are then found by their
caption rather than by what is in them.

**The free search tier refuses.** There is one per subscription and you have
one already. Use `--tier basic`, and delete it when you stop.

**Files are in `ingest-poison`.** They failed five times. Read why:

```bash
az functionapp log tail --name <your function app> --resource-group <your group>
```

**A video indexed nothing.** Only the sound is read. A video where nobody
speaks has no transcript, which is correct and looks like a failure.

**A photograph indexed nothing.** OCR reads words. A picture with no words in
it has nothing to read.

**One request in twenty takes fifteen seconds.** Flex Consumption gives a
Python app one web request an instance unless told, so requests that overlap
each start a new instance, and each new instance loads the library before it
answers. 06 sets `VX_QUERY_AT_ONCE`, 16 unless `settings.env` says otherwise,
on the query app, and `06 --settings-only` sends a change. In Application
Insights, many instances with one slow request each is this.

## What is deliberately left out

- **Key Vault.** Keys go into the Function App's settings, which is fine for a
  test that gets deleted. Production uses Key Vault references.
- **Always-ready instances, unless asked for.** Flex keeps none, so the first
  call after a quiet spell waits for a start. `VX_QUERY_ALWAYS_READY=1` keeps
  one instance of the query app ready, which costs money by the hour rather
  than by the call.
- **API Management.** `vectrixdb check --url` tests a deployment through a
  gateway, and that is worth doing after this works.
- **Anything outside one resource group**, so that deleting is one command.

## What other people can call

The app hosts `vectrixdb.api.server.create_app`, so everything the library
serves is live: search in eight shapes, documents and their chunks, a
document's provenance, a collection's quality and policy and builds, the
evaluation runs, the audit trail and the access log, and `/api/v1/keys` to
issue somebody a scoped key that expires.

Two addresses are worth knowing before the rest:

| | |
| --- | --- |
| `/health` | the library's. Needs no key. A reply of any kind proves the app loaded, which is the failure worth catching. |
| `/health/wiring` | ours. What this deployment was given: collections, policy, who reads what, a yes or no per service. Opens nothing. |
| `/api/v1/info` | answers from the database, so `"storage_backend": "azure_search"` in its reply is the proof that Azure AI Search was reached. |

The door is the library's, not Azure's. The Functions auth level is anonymous
on purpose: a function key is one shared secret with no scope and no expiry,
and the library has scoped keys, tokens, an identity provider and a rate limit
already. One consequence catches people out: who may retrieve from a
collection is its record's rule, and a key is nobody in particular, so a bare
key is refused unless it is the server's own or one made for that collection,
and a caller who signed in is served when the record names them, an admin
told why with a 403 otherwise. The rule is stored on the collection rather
than applied by whoever opened it, which is why that holds for every caller.

Signing in is on at the query app's address when `VX_SIGNIN=yes`, which is
the default. It has to be said to the app: a Function App hosts the API itself,
and the library's refusal to listen with no key and no sign-in is only in
`vectrixdb serve`, so an app given neither answers anybody on every route but
reading a collection with a policy. The app sends no mail, so the first person
is added from this machine with `--add-person`, which prints their one-time
link in that terminal. The link opens a page on the query app, so start it
first. After that, admins add people on the dashboard's Access page.
Somebody who has not signed in is a guest when `VX_GUESTS=yes`, the default:
the overview, every collection's name and size, and the evaluations, never a
search.

Signing in is for the team that owns the platform; the people who use the
collections are named by each collection's policy. `VX_SIGNIN_PASSWORDS=yes`
asks for a password with the code, after the work email, for while there is
no single sign-on. `VX_SIGNIN=sso` is the enterprise way: the company's single
sign-on, or a work email and the code from an authenticator app each person
may add from their account, with no passkeys, from one People list, which `VX_SIGNIN_USERS` fills and admins keep
on the Access page. `VX_SIGNIN=sso-only` is the company's sign-in alone; while `VX_OIDC_ISSUER`
and `VX_OIDC_CLIENT_ID` are both empty the button checks, says single sign-on
is not configured, and opens the email way: the People list signs in with a
work email and a code, with no passkeys, until they are set. 06
will not send either without somebody named, `VX_SIGNIN_USERS` or
`VX_OIDC_ALLOWED_EMAILS`, because not everyone in a security group may sign
in. `VX_OIDC_API_AUDIENCE` and `VX_OIDC_TOKEN_ROLE` let another team's app
call the query app as the person using it, and `VX_SSO_RECHECK_DAYS` asks for
single sign-on again every so often. With single sign-on, `VX_BREAK_GLASS=yes`
and its three settings turn on emergency sign-in at `/dashboard/#/break-glass`
until the time given, for the day single sign-on is down: a username and a
password, no code. `06 --new-break-glass` makes the password, keeps it in the
key vault `VX_KEY_VAULT` names as `vx-break-glass`, and writes only its hash
to `settings.env`, showing neither. A password works for one emergency, so it
is run again before the next.

Behind a gateway such as API Management, `VX_GATEWAY_URL` is the address
people type, `VX_QUERY_PREFIX` the path every route lives under, and
`VX_QUERY_GATEWAY_PATHS` each route's own path, as the gateway team hands them
over; `VX_TRUSTED_PROXIES`, `VX_KEY_HEADER` and `VX_TOKEN_HEADER` follow. 06
has the library read them before anything is sent, so a path no route falls
under stops there. The dashboard of your own reaches the query app the same
way with `UPSTREAM_PREFIX` and `UPSTREAM_GATEWAY_PATHS` in its Backend's
`.env`.

The company's look, `VX_BRAND_NAME`, `VX_BRAND_LOGO`, `VX_BRAND_ACCENT`,
`VX_BRAND_WORDMARK`, `VX_BRAND_COPYRIGHT` and `VX_BRAND_PALETTE`, is set once
on the query app and shown by both dashboards. A function app keeps no files,
so 06 sends the logo and the palette inside their settings, as `data:`
addresses, after the library has read them and found nothing it would refuse.
The dashboard of your own asks the query app for the brand and forwards
`/brand.json`, `/brand.css` and `/brand/` to it.

`/api/v1/ws/status` is a WebSocket and Azure Functions does not serve one.

## Pictures inside a PDF

A collection opened with a describer keeps the pictures inside every document
it reads, whether that document came off a disk or out of a blob.
`03_create_ai_services.py` makes an Azure AI Vision resource and
`06_create_main_function_app.py` puts it in the app's settings; from there the library does the rest, and the
description it writes is kept in the Markdown so it is never paid for twice.

Vision rather than a GPT model, deliberately: describing a picture with a
language model needs quota a new subscription does not have, and Image
Analysis needs none. It answers two useful things in one call, a sentence
saying what the picture is and the words printed inside it, and the words come
back in the order a person reads them rather than the order the service
emitted them, which on a chart is the difference between a title with its axis
labels and a bag of numbers.

## A page with no text layer

A scanned page has no text to extract, and a PDF full of them used to go in
almost empty. The quality gate noticed and said so, and nothing acted on it.

Now the collection is opened with `page_ocr`, and the library sends
Document Intelligence the pages whose text layer is under a couple of dozen
characters, and only those. A born-digital report costs nothing; a report with
two scanned inserts costs two pages; a page with no ink on it is never sent at
all, because a reader shown an empty page invents text for it. Which pages
were read that way is recorded, so a chunk can say whether its own was.

This is a hook on the PDF reader rather than an extractor registered for
`.pdf`, and that is the whole point: registering one replaces the reader and
takes every figure in the document with it, silently.

One limit worth knowing: a Markdown file's pictures are separate files beside
it, so a `.md` fetched on its own has its figure lines and not its pictures.
A PDF carries its pictures inside, which is why the two TD reports work.
