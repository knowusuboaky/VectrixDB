# The cloud, on this machine

This folder is a mirror. What is under `blob/` is laid out exactly as the
storage account is, account then container, with `data_db` standing for the
account whatever it is called. What is under `cosmosdb/` is laid out as the
Cosmos account is, database then container, and `data_db` is the database
that holds the records. So a push is a copy and nothing has to be worked out:

    .local/blob/data_db/ingestion/raw/financial/td/td-annual-report-2025.pdf
    .local/blob/data_db/ingestion/raw/media/male.wav
    .local/blob/data_db/ingestion/raw/misc/office.png

goes to the `ingestion` container as

    raw/financial/td/td-annual-report-2025.pdf
    raw/media/male.wav
    raw/misc/office.png

The first folder under `raw/` is the collection a file lands in, and any
folder under that is only part of the document's name. Nothing is looked up
and nothing is configured. To send a file somewhere else, move it.

## What is here

| | |
| --- | --- |
| `blob/data_db/ingestion/raw/` | the files that go up, and the ones that are already there |
| `blob/data_db/evals/golden_dataset/` | the golden dataset, one file for the whole walkthrough |
| `cosmosdb/data_db/collection_records/` | each collection's record, `<collection>.json`: its name, its path and its policy |
| `later/` | held back on purpose, dropped by step 07 to watch the trigger fire |
| `sources.json` | where each file comes from, and which collection it belongs in |

`later/` is the one folder that is not a mirror of anything. The files in it
have not been dropped yet, so they are not in the container, and a mirror that
showed them would be lying. Step 07 moves them into `blob/data_db/ingestion/raw/` and
uploads them, and after that the mirror is the container again.

## The two pushes

    python 04_push_local_to_blob_cosmosdb.py                     # fetch what is missing; the records, then the files
    python "07_push_new_files_from local_to_blob_cosmosdb.py"    # drop later/; the records, then what is new

Step 04 goes up before the app exists. Step 07 lands on a deployment that is
already running, which is what shows the trigger working: nothing calls the
function, a blob is written, Event Grid notices, a message lands on the queue,
and the function wakes on its own. Two batches also mean two builds, which is
what puts a second bar on the chunks by build chart.

Both skip a file the container already has at the same size, and a record
Cosmos already has the same, so both are safe to run again. Pass `--again` to
send them anyway.

## The records

`cosmosdb/data_db/collection_records/financial.json` goes to the Cosmos
account's `data_db` database, container `collection_records`, as the item
`collection.financial`. It says who may retrieve from the collection, by
security group, by address, or everyone who signs in, and every server
reads it from there. The record is the policy and nothing else: the dashboard's Policy tab edits the same record a push writes, and `--again` writes the file's over it. These
files are committed, because they are the walkthrough's own rules and not
somebody else's data. The walkthrough's README says what each key means.
