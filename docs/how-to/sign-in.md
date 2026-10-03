# Sign people in

Two API keys tell the server that *somebody* holds a key. They cannot tell it
who, so they cannot give one person less than another, and nothing a key does
can be traced to a person. Sign-in fixes that: people sign in to the dashboard
as themselves, a role decides what each may do, a collection's entitlement
policy judges their searches, and every read is recorded under their name.

There are three ways to set sign-in:

| `VECTRIXDB_SIGNIN` | The sign-in box | Passkeys |
| --- | --- | --- |
| `oidc` | Single sign-on alone | None |
| `oidc,email` | Single sign-on, or a work email and a code | None |
| `email` | A passkey, or a work email and a code | Kept |

None needs a password, and passwords stay off unless you turn them on. With
each, the People list says who may sign in and with what role, and
[emergency sign-in](#emergency-sign-in) is there for the day the usual way in
is down, whichever it is. On a developer's own machine,
[Developer Access](#developer-access) tries each role with no identity
provider at all.

Signing in to the server and using a collection are two questions. Sign-in is
for the team that runs the platform, and its list says who that is. Using a
collection is judged by the collection's own policy, whoever asks: a person in
the dashboard, or an app calling the API as one. A policy that names security
groups names people too, because not everyone in a group may read.

```bash
pip install "vectrixdb[api,signin]"
```

## What is always needed

```bash
export VECTRIXDB_SIGNIN_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export VECTRIXDB_PUBLIC_URL="https://vectors.example.com"
```

The secret signs session cookies and seals the authenticator secrets the
server keeps. Each job gets its own key derived from the secret, so neither can
stand in for the other. It has to be the same on every restart, and it belongs
in a secrets manager, not a file beside the data.

Every secret setting has a `_FILE` twin that reads the value from a file,
which is how Docker and Kubernetes secrets arrive:

```bash
export VECTRIXDB_SIGNIN_SECRET_FILE=/run/secrets/vectrixdb_signin
```

Setting both is refused when the server starts, and so is a file that cannot be
read or is empty. The same goes for `VECTRIXDB_OIDC_CLIENT_SECRET_FILE` and
`VECTRIXDB_SMTP_URL_FILE`.

**Rotating the secret.** Put the new one first and the old one after a comma,
and restart:

```bash
export VECTRIXDB_SIGNIN_SECRET="<new secret>,<old secret>"
```

As it starts, the server seals every authenticator secret again with the new
one. Sessions signed with the old one keep working while it is listed. Take the
old one out once the longest session has run out, eight hours by default. Only
drop it after one start with both: a server that finds a sealed secret it
cannot open refuses to start and says to put the old one back, rather than
quietly lock everybody out.

The public address is the one people type. The identity provider sends people
back to it and the sign-in email links to it. It is configuration and never
taken from a request, because a request can claim to be any address it likes.
It must be `https`, except `http://localhost` on a laptop.

## Where sign-in keeps its state

By default everything sign-in remembers is in one folder beside the data,
`<database path>/auth/`, so `vectrixdb serve --path /data` keeps it in
`/data/auth`. `VECTRIXDB_AUTH_PATH` gives the folder a place of its own, which
it needs when the collections live in a service and the local disk does not
last.

| File | What is in it |
| --- | --- |
| `signin.db` | People and their roles. Each person's passkeys (public keys only), authenticator secret (sealed with the sign-in secret), recovery codes and password (hashed). Sessions, stored under a hash of the cookie. One-time links and passkey challenges (hashed, and minutes long). Lockout counters. API keys for scripts (hashed). Which collections are shared with guests. |
| `access.jsonl` | The access log. `VECTRIXDB_ACCESS_LOG` moves it. |

Nothing in the folder signs anybody in by being read: passkeys are public
keys, codes and passwords are hashes, and the authenticator secrets are sealed
with a key from the sign-in secret, which is why that secret lives somewhere
else.

Put the folder on the same persistent volume as the data and back the two up
together. Losing it signs everybody out and sends everybody on the email list
back to a first visit. A SQLite file is for one server: two servers writing one
over a network share can corrupt it.

**Several servers, or a disk that does not last.** Keep sign-in in a database
the servers share instead:

| Where | `VECTRIXDB_SIGNIN_STORE` | Install |
| --- | --- | --- |
| A SQLite file, the default | unset, or `sqlite:///path/to/signin.db` | nothing more |
| PostgreSQL | `postgresql://vectrixdb@db.internal:5432/vectrixdb?sslmode=require` | `vectrixdb[postgres]` |
| Azure Cosmos DB | `cosmos://<account>.documents.azure.com/<database>/<container>` | `vectrixdb[azure]` |
| Amazon DynamoDB | `dynamodb://<table>?region=ca-central-1` | `vectrixdb[aws]` |

A password or account key never goes in the address, which is printed when the
server starts: set it in `VECTRIXDB_SIGNIN_STORE_KEY`, or read it from a file
with `VECTRIXDB_SIGNIN_STORE_KEY_FILE`. Cosmos DB with no key signs in as the
machine (a managed identity in Azure, `az login` on a laptop), and DynamoDB
always uses the usual AWS chain: the task's or the instance's role, or a
profile. The address itself can come from `VECTRIXDB_SIGNIN_STORE_FILE`.

On its first start the server makes what it needs, where its identity may:

- PostgreSQL: a table, `vectrixdb_signin`, or the name after `?table=`. A
  user that may only read and write it needs nothing more once it is there;
  to make it yourself, run what the refusal prints, which is:

    ```sql
    CREATE TABLE IF NOT EXISTS vectrixdb_signin (kind TEXT NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL,
        version BIGINT NOT NULL, ix1 TEXT, ix2 TEXT, expires DOUBLE PRECISION, PRIMARY KEY (kind, key));
    CREATE INDEX IF NOT EXISTS vectrixdb_signin_ix1 ON vectrixdb_signin (kind, ix1);
    CREATE INDEX IF NOT EXISTS vectrixdb_signin_ix2 ON vectrixdb_signin (kind, ix2);
    CREATE INDEX IF NOT EXISTS vectrixdb_signin_expires ON vectrixdb_signin (expires);
    ```
- Cosmos DB: the database and the container, partitioned by `/kind`, with time
  to live on and no default. Every item that expires carries its own.
- DynamoDB: an on-demand table keyed on `kind` and `key` (strings), with the
  indexes `ix1` on `g1` and `ix2` on `g2`, and time to live on `expires_at`.

Where it may not, as a production identity usually may not, it refuses to start
and names exactly what to make. Anything that must happen once (a code, a
recovery code, a link, a passkey challenge) happens once however many servers
are asked at the same moment, and a change one server makes is never lost to a
change another made at the same time.

The start-up message says where sign-in is kept, and never shows a password:

```text
  Sign-in: email
  Sign-in store: PostgreSQL db.internal:5432/vectrixdb
```

`vectrixdb people` works on the same store as the server, run with the same
settings.

**A file from an earlier build.** Its first start on this release copies the
file's tables into the new form, keeps the file as it was beside it as
`signin.v1.db`, and says so in the log. Nobody is signed out. Delete the copy
once people can sign in; until then it deserves the same care as the file.

## Single sign-on

One OpenID Connect integration covers Entra ID, Okta, Google Workspace, Auth0,
Keycloak and anything else that publishes
`/.well-known/openid-configuration`.

```bash
export VECTRIXDB_SIGNIN=oidc
export VECTRIXDB_OIDC_ISSUER="https://login.microsoftonline.com/<tenant id>/v2.0"
export VECTRIXDB_OIDC_CLIENT_ID="<application id>"
export VECTRIXDB_OIDC_CLIENT_SECRET="<secret>"
export VECTRIXDB_OIDC_ROLE_MAP='{"<admins group id>": "admin", "<analysts group id>": "operator"}'
export VECTRIXDB_OIDC_DEFAULT_ROLE=viewer     # leave unset to refuse everybody else
vectrixdb serve --host 0.0.0.0
```

Register `https://vectors.example.com/auth/oidc/callback` with the provider as
the redirect address, exactly. Providers compare it letter by letter, and a
mismatch shows up after the person has signed in, which reads like an outage.

The button says **Continue with SSO**, whichever company runs the provider;
`VECTRIXDB_OIDC_LABEL` changes it. On the way to the provider and back, the
dashboard shows a full-screen **Checking your access** page while it checks the
person's sign-in, their security group and the address list. A refusal says
why and offers **Try another account**, which asks the provider for its account
picker.

Second factors, lost phones, leavers and conditional access stay with the
provider. When somebody is removed from a group there, they lose the role here
at their next sign-in, and a sign-in lasts eight hours
(`VECTRIXDB_SESSION_HOURS`).

**Security groups decide the role.** The provider must put groups in the
identity token (in Entra ID: Token configuration, add the groups claim). A
person in several mapped groups gets the highest role among them. A person in
none gets the default role, or is refused if there is none.

**Who may sign in: a group and a list.** A security group alone is not
enough: it is usually broader than the people who should run the platform.
Single sign-on is checked against the People list, the same list the email way
in uses, and somebody on it has the role their record gives, whatever their
groups would give. Their group is still asked: somebody on the list in no
mapped group, with no default role, is refused. Somebody turned off on the list
is refused as well, with "your access here is turned off". To let in more
people beside the list, with their groups' role, name them or their domain:

```bash
export VECTRIXDB_OIDC_ALLOWED_EMAILS="ada@example.com, @legal.example.com"
```

An entry that starts with `@` covers a whole domain, and commas or semicolons
separate entries. Somebody on neither list is refused with "You signed in, but
your address is not on the list for this server." An address the provider says
it has not checked, `email_verified: false`, is on no list.

With nobody on the People list and nothing in `VECTRIXDB_OIDC_ALLOWED_EMAILS`,
the server will not start. Put the first people on the list with
`VECTRIXDB_SIGNIN_USERS="ada@example.com:admin"`, or on the server with
`vectrixdb people add ada@example.com --role admin`; both work with single
sign-on alone, and after that admins manage the list on the **Access** page.
`VECTRIXDB_OIDC_ALLOWED_EMAILS=*` says outright that the groups decide on
their own, and the server warns about it at every start.

With single sign-on the only way on, the sign-in box is its button and one
line, "Your work account signs you in. There's no password here."

**The same groups can feed an entitlement policy.**

```bash
export VECTRIXDB_OIDC_PRINCIPAL_CLAIMS='{"clients": "groups", "department": "department"}'
```

Each entry names a principal attribute and the claim it comes from. With the
line above, a collection whose policy is `Overlap("client_id", "clients")`
shows a person the documents whose `client_id` is one of their groups, and
nothing else. See [Restrict what a search can see](entitlements.md).

**Groups that do not fit in the token.** Entra ID leaves the groups out when
there are too many and says so in the token. The server then fetches them
from the address you give it, with the person's access token, reading the
`id` of each entry in `value` and following `@odata.nextLink`:

```bash
export VECTRIXDB_OIDC_GROUPS_URL='https://graph.microsoft.com/v1.0/me/transitiveMemberOf?$select=id'
export VECTRIXDB_OIDC_SCOPES='openid profile email GroupMember.Read.All'
```

The address and the scope are Microsoft's to define, so check them against
Microsoft's documentation for your tenant; they are configuration here and not
built in for that reason. With no address set, a person who hits the limit is
refused with a message that says why. They are never signed in with an empty
group list, which would quietly give them the wrong role.

**A certificate instead of a client secret.** A secret can leak from wherever
it is kept. Give the server a private key instead and register its certificate
with the provider: the server then proves who it is by signing a short
assertion each time (`private_key_jwt`, RFC 7523), and nothing that could be
replayed ever travels.

```bash
export VECTRIXDB_OIDC_CLIENT_KEY_FILE=/run/secrets/vectrixdb_sso_key    # PEM, no passphrase
export VECTRIXDB_OIDC_CLIENT_CERT_FILE=/run/secrets/vectrixdb_sso_cert  # for Entra ID
export VECTRIXDB_OIDC_CLIENT_KEY_ID="<key id>"                          # for Okta or Keycloak
```

The key is RSA of 2048 bits or more, or EC on P-256 or P-384. Entra ID finds it
by the certificate's thumbprint, so give it the certificate; Okta and Keycloak
find it by the id it was registered under. A key and a client secret together
are refused, and so are a certificate that is not the key's and one that has
expired. In its last thirty days, every start warns you to register the
certificate's successor.

**Admins through single sign-on only.** With both ways on,
`VECTRIXDB_ADMINS_USE_SSO=on` refuses an admin who signs in any other way. It
is off by default, because an admin's own code is one way in when the
identity provider is down. Emergency sign-in, below, is the other.

**Before the provider is set up.** `VECTRIXDB_SIGNIN=oidc` with
`VECTRIXDB_OIDC_ISSUER` and `VECTRIXDB_OIDC_CLIENT_ID` both empty starts, so a
server can be put up before its app registration exists. The sign-in box
draws the single sign-on button alone. Pressed, it checks, says single
sign-on is not configured, and opens the email way by itself: people on the
People list sign in with their work email and the code from their
authenticator app, set up from the link `vectrixdb people add` prints.
Set the two and restart, and the email way is gone: single sign-on alone, as
asked. One of the two without the other is a mistake, and the server says so
at start. With `oidc,email` the email way is in the box from the start.

**No passkeys beside single sign-on.** With `oidc` or `oidc,email` nobody
keeps a passkey: the page offers none, and the passkey routes answer 404, as
anything the server does not have. Passkeys are for `VECTRIXDB_SIGNIN=email`.
A passkey made before single sign-on was turned on stays in the store and
signs nobody in; its owner sets up an authenticator app from a new link.

## Both ways, from one list

```bash
export VECTRIXDB_SIGNIN=oidc,email
export VECTRIXDB_SIGNIN_USERS="ada@example.com:admin,sam@example.com:operator"
export VECTRIXDB_SSO_RECHECK_DAYS=30     # optional
```

The enterprise shape. One People list says who may sign in, whichever way they
come, and what their role is.

- **The first time** is single sign-on. Nobody needs a link by email.
- **A way of their own, if they want one.** From **How you sign in** in the
  account menu they add an authenticator app, and sign in with their work
  email and its code next time, even while the provider is slow or down. They may remove it again
  at any time, the last one too, because single sign-on still lets them in;
  somebody single sign-on has never let in keeps their only way. A change that
  matters takes their own code as well as single sign-on.
- **One record.** Changing somebody's role on the Access page, turning them
  off, or taking them off the list ends every session they have, however they
  came in.
- **Asking the provider again.** A code is the person's, not the
  company's, so a leaver's would go on working. `VECTRIXDB_SSO_RECHECK_DAYS`
  lets a code work only for somebody who signed in with single
  sign-on within that many days. After that the sign-in box says to use single
  sign-on first, and then their own way works again. Somebody it has never let
  in is asked to use it before anything is set up.

Leave `VECTRIXDB_ADMINS_USE_SSO` off for this shape: an admin's own code is
what gets them in while the provider is down.

## Emergency sign-in

For the day the usual sign-in is down, and only for that day: one named
admin and a password kept in a key vault. No code is asked for. It goes with
every `VECTRIXDB_SIGNIN`: the identity provider can be down, and so can an
admin's only device.

```bash
vectrixdb break-glass hash --admin emergency.admin     # asks for the password twice, unseen, and prints its hash
```

```bash
export VECTRIXDB_BREAK_GLASS=on
export VECTRIXDB_BREAK_GLASS_UNTIL=2026-09-28T02:00Z        # UTC; it turns itself off then
export VECTRIXDB_BREAK_GLASS_ADMIN=emergency.admin
export VECTRIXDB_BREAK_GLASS_PASSWORD_HASH='scrypt$32768$8$1$...'
```

- **The password is never a setting.** It is made for this, 24 characters or
  more, and kept in a key vault. What the server is given is its hash, which
  checks a password and cannot be read back as one, so somebody who reads the
  settings has not read the password. A password put in the settings,
  `VECTRIXDB_BREAK_GLASS_PASSWORD`, stops the server from starting.
- **One password an emergency.** A password goes on working while the
  emergency it was first used in lasts, and a later
  `VECTRIXDB_BREAK_GLASS_UNTIL` given to the same emergency is taken. Once that
  emergency is over, by its time or by being turned off, the password is spent
  on every server: it is refused, and a server set with it will not start.
  Make a new one before the next, so whoever saw one during an incident holds
  nothing afterwards.
- **The page** is `/dashboard/#/break-glass`, one fixed address, written here
  and nowhere else: the sign-in page never links to it. Nothing about it is
  secret but the password, because an address is not a lock. While it is off,
  the page says **Emergency sign-in is off**, and the route behind it answers
  404, the same as a route that is not there.
- **Both at once.** The username and the password are checked together; a
  wrong one refuses both without saying which. Five wrong tries shut the door,
  as everywhere else.
- **What stands in for a second step.** It is off unless an operator turns it
  on, it turns itself off at a time given in hours, every use is recorded, and
  the vault records every read of the password. Alert on both.
- **Recorded.** Each sign-in writes `break_glass_used` to the access log, and
  every read and search after it is recorded under `break-glass:<admin>`. A
  change that matters asks for the password again.
- **It ends.** Its sessions last no longer than it is on. When the time in
  `VECTRIXDB_BREAK_GLASS_UNTIL` passes, or it is turned off, every session it
  opened ends at its next request. A time already past starts the server with
  it off and says so.
- **Admins are told.** While it is on, every page shows admins **Emergency
  sign-in is on until 02:00 UTC. Turn it off when SSO is back.**

It needs single sign-on on, `VECTRIXDB_SIGNIN=oidc`, and every part at once: a
missing or wrong one stops the server from starting, with the setting named,
so nobody learns on the bad day that the way in was never set up. Check the
settings before the restart with `vectrixdb check`.

## Developer Access

For trying each role on a developer's own machine, in place of single sign-on.
It goes with `VECTRIXDB_SIGNIN=oidc` or `oidc,email`, or on its own; beside
`email` alone the server will not start with it.

```bash
export VECTRIXDB_PUBLIC_URL=http://localhost:7337
export VECTRIXDB_SIGNIN_SECRET_FILE=~/.vectrixdb/signin-secret
export VECTRIXDB_DEVELOPER_ACCESS=on
export VECTRIXDB_DEVELOPER_USERS="admin.user:admin,operator.user:operator,viewer.user:viewer"
export VECTRIXDB_DEVELOPER_PASSWORD_FILE=~/.vectrixdb/developer-password
```

- **This machine only.** The server will not start with it unless the public
  address is this machine's, and it answers only a connection that comes from
  this machine: a forwarded header claiming so counts for nothing. Anywhere
  else its address, `/auth/developer`, answers 404, the same as a route that is
  not there.
- **The page.** Nothing in the sign-in box names it. The single sign-on button
  is pressed as ever, and on this machine the page says **Local development
  detected**. With no provider set up it says **Single sign-on not
  configured** and opens **Developer Access**: a username and the password.
  With one set up it offers both, single sign-on first.
- **No default password.** The accounts and their password are in the
  settings, and five wrong tries shut the door, as everywhere else.
- **What it opens.** A session with the role the settings give the account,
  recorded under the account's name, that works from this machine only and
  ends when Developer Access is turned off. A change that matters asks for the
  password again.

## An email allowlist

For a team with no identity provider.

```bash
export VECTRIXDB_SIGNIN=email
export VECTRIXDB_SMTP_URL="smtp://user:password@smtp.example.com:587"
export VECTRIXDB_MAIL_FROM="vectrixdb@example.com"
vectrixdb serve --host 0.0.0.0
```

**The first admin** is added on the server itself, so nobody who merely finds
the port can claim the job:

```bash
vectrixdb people add ada@example.com --role admin --path /data
```

It prints a link that works once, for fifteen minutes, for whoever is at that
terminal. While there is no admin, the server's start-up message prints this
command, and never a link. `vectrixdb people reset`, `list` and `remove` work
the same way, and `reset` is the way back in for an admin who has lost both
their device and their recovery codes. Run them with the same settings and
`--path` as the server. `VECTRIXDB_SIGNIN_USERS="ada@example.com:admin"` still
works too: it adds whoever is missing when the server starts and changes
nobody. After that, admins add people and give them roles on the dashboard's
**Access** page.

What a person goes through:

1. **The first visit.** They type their address and get a link by email. The
   link works once, for fifteen minutes. It opens a page with a button, and
   the button is what spends the link, because mail scanners open every link
   in a message. They choose how they will sign in:
    - **A passkey**, the recommended one. It is made on the computer they are
      using and unlocked with that computer's PIN. It never leaves the
      computer, and a look-alike sign-in page cannot use it.
    - **An authenticator app.** They scan a QR code with Microsoft
      Authenticator, Google Authenticator, 1Password or any other, and type the
      6-digit code it shows.

    Then they are given ten recovery codes to keep.
2. **Every visit after.** **Sign in with a passkey**, or their address and the
   6-digit code.
3. **A lost device.** A recovery code signs them in once in place of a code.
   With none left, an admin presses **Reset sign-in** on the Access page: that
   forgets their passkeys, authenticator, password and recovery codes, signs
   them out everywhere, and their next visit starts again from a new link. The
   reset is in the access log.

A passkey requires the device to check that it is the person, which the
dashboard asks for as the computer's PIN. The browser and the operating system
choose how that check is made; the server requires that it was made, and
refuses a passkey that says otherwise.

Anybody signed in can manage their own ways in from **How you sign in** in the
account menu, top right: add or remove passkeys (never the last way in), set up
a new authenticator (the old one keeps working until the new one is
confirmed), make fresh recovery codes, and see every browser they are signed in
on, each of which they can sign out.

There is deliberately no "forgot my code, email me a way in". A link like that
would be a way past the second step for anybody who can read the mailbox.

Any SMTP server works, which covers a company relay, Amazon SES and the
hosted services. STARTTLS is required on `smtp://`; use `smtps://` for port
465. With no mail server configured on a laptop, where the public address is
`localhost`, the email is written to the server's log. On a server that others
can reach, it is not sent and not logged, because whoever reads the log could
then enrol as anybody; the log says to set `VECTRIXDB_SMTP_URL` or to make the
link with `vectrixdb people reset`.

**Wrong codes.** A code is accepted for the thirty second step either side of
now, and once only. Five wrong codes shut that address out: for fifteen minutes
the first time, then an hour, four hours and a day, and the person is emailed
when it happens. Only a real sign-in, or a day with no lock, starts the count
again. Recovery codes have their own count, and one network address gets fifty
wrong tries across every address before it is stopped. The reply to "continue
with email" is the same for every address, listed or not, so the page teaches
nobody who has access.

### Passwords, if you want them

```bash
export VECTRIXDB_SIGNIN_PASSWORDS=on
```

A password is then asked for with the code, never instead of it. Somebody who
chooses the authenticator app sets one on the first visit; it is at least
twelve characters, is not a common one, and is stored with scrypt. A wrong password and a wrong code get the same answer in
the same time, so a guess does not learn which was wrong. **Forgot password**
emails a link, and setting a new one still needs a current code. A passkey
needs no password: it is already something the person has, unlocked by
something they know.

After the work email, the sign-in box then asks for the password and the
code on one form, with **Forgot your password?** and **Use a recovery code**
under it.

### Passkeys only

```bash
export VECTRIXDB_SIGNIN_REQUIRE=passkey
```

People on the list then sign in with a passkey and nothing else. A new person
makes one on their first visit and is never shown an authenticator. A code from
somebody who has a passkey is refused, and they are told why only once the code
has proved right, so a guess learns nothing. Recovery codes still work, because
a lost device is what they are for, and the fresh check before a change that
matters is a passkey too.

Switching a server over locks nobody out. Somebody who has only an
authenticator signs in with a code as before and is taken straight to making a
passkey: until they have one, making it, their own page and signing out are all
they can reach. Nobody can remove their last passkey. It needs the email way in
on, and it cannot be combined with passwords.

## Confirm it's you

Some changes ask for a fresh check when the last one was more than ten minutes
ago: deleting a collection, sharing one, adding or removing people, making or
revoking an API key, and changing your own ways in. The dashboard shows
**Confirm it's you**, takes a passkey or a code, and then makes the change.
People who came through single sign-on go back through the provider, or use a
code of their own when they have one. Developer Access asks for its
password.

Over the API the refusal is a `403` whose `data` has `"step_up": true` and the
ways that will do, and `POST /auth/step-up` with a current code gives the
session a fresh check.

## Guests

```bash
export VECTRIXDB_GUESTS=on
```

With guests on, somebody who has not signed in can look around before they
do: the Overview, the Collections and the Evaluate page. What is inside a
collection is for people who sign in, and a collection's policy decides which
of them may search it.

A guest:

- sees every collection's name, description and size, and how many people
  its policy names, never who;
- sees the Evaluate page, how every setup scored, which says nothing about
  what is stored;
- never searches: every search route answers a guest with the `401` that
  sends them to sign in, before the collection's name is looked up;
- cannot write, open a chunk or a document, download the golden questions,
  or open the audit trail or the access log.

Guests are a server setting and not a switch in the page, so one click cannot
open a private server to the internet.

## Masking identifiers

Email addresses, phone numbers and card numbers are masked in what every
collection shows people. It is not a switch: every reply from a collection's
routes is masked on its way
to a person or a guest: search results, a chunk opened with **Show text**, a
kept document, quality and provenance excerpts, and every metadata value in
them. An address keeps its first letter and its domain, `a•••@example.com`; a
phone number and a card keep their last four digits, `•••-•••-0199` and
`•••• •••• •••• 1111`. A card is thirteen to nineteen digits that pass the
Luhn check, so an order number is not mistaken for one. Dates, times, decimals,
versions and ids are left alone, and an id is never masked, so the page can
still open what it was shown.

An API key is sent the text as stored, the server's own key included, because
a script feeding a pipeline needs it. The masking is in `vectrixdb.masking`
for a program that wants the same rules on its own output.

It lowers casual exposure. It is not a guarantee:

- A shape it does not know is shown as written: a name, an account number
  written with letters, an identifier from somebody else's format.
- The index holds the text as it was written, so a search for a phone number
  still finds the chunk that has it, and the masked result shows where it was.
- It masks what the server sends to people. Whoever holds an API key reads the
  text as stored, so it is only as good as the list of who holds the keys.

The stronger form is to mask a document before it is indexed, so the index
never holds the identifier at all. That is the extraction service's `?mask=1`
and `/mask`, with an engine that reads meaning and not only shape: Presidio in
the process, Azure AI Language, or Amazon Comprehend, chosen by
`VECTRIXDB_MASKING_ENGINE`, with the patterns running after it. See
[Run an extraction service](extraction-service.md).

A collection deleted and made again under the same name starts unmasked,
because the setting belongs to the moment the collection was made.

## Roles

| May | viewer | operator | admin |
| --- | --- | --- | --- |
| See collections, their size, health, builds and settings | yes | yes | yes |
| See evaluation runs: every setup's scores, and ids, never text | yes | yes | yes |
| List chunk ids and where a chunk came from, with the text hidden | yes | yes | yes |
| Open a chunk to read its text and metadata, one at a time, each one recorded | | yes | yes |
| Open a whole stored document | | if given | yes |
| Search | | yes | yes |
| Add, change and remove chunks and documents; create a collection; rebuild | | yes | yes |
| Manage their own ways to sign in and where they are signed in | yes | yes | yes |
| Delete a collection, clear the cache | | | yes |
| Choose who may search a collection | | | yes |
| Read the audit trail and the access log | | | yes |
| Add and remove people, change roles, reset sign-in | | | yes |
| Make and revoke API keys for scripts, and scope them | | | yes |
| Download the golden questions an evaluation run used | | | yes, signed in as themselves |

**Golden questions.** An evaluation run holds scores and ids, but the golden
file it read holds the questions' text and their reference answers, so
downloading it is its own action, `evaluation.golden`. Only a person may take
it, an admin signed in as themselves, and never a key: the server's own
`VECTRIXDB_API_KEY` has the admin role, but a key is a script, and the access
log should name who took the questions. See
[Evaluate every setup](evaluate-setups.md#read-the-results).

**Whole documents.** A stored document is a larger disclosure than a chunk of a
few hundred characters, so opening one is its own permission, `document.read`.
An admin holds it. An operator holds it when an admin ticks **Whole documents**
beside their address on the Access page, or, with single sign-on, when they are
in a group named in `VECTRIXDB_OIDC_GRANT_MAP`:

```bash
export VECTRIXDB_OIDC_GRANT_MAP='{"<legal group id>": ["document.read"]}'
```

It is the only thing that can be given to a person on top of their role. Giving
it to a viewer does nothing, and taking it away signs the person out, because a
session carries what it was opened with.

**Text is shown when asked for.** The dashboard lists chunks by id, source and
quality. A chunk's text is one request for that chunk, made by pressing **Show
text**, and search results carry a 200 character excerpt cut on the server. So
the access log can say which chunks a person opened, not only that they looked
at a collection.

The table lives in one place, `vectrixdb.signin.roles`, and the dashboard's
Access page is drawn from it, as is the [REST API reference](../reference/rest-api.md),
which says for every route the action it needs and who holds it. Denial is the default twice over: a route the
table does not place can be called only by an admin, so an endpoint added
later is closed until somebody decides who it is for, and a role the table
does not know holds nothing.

A request never names its own role. The role is settled at sign-in and kept in
the session record on the server.

## What every request passes

In this order, and the order matters:

1. **Who.** An API key or a session cookie. Neither: `401`, or with guests on,
   a guest.
2. **Forgery.** A request that changes something and arrives on a cookie must
   also carry the session's token in `X-CSRF-Token`. Missing or wrong: `403`.
   It comes second so that somebody whose session has merely run out gets the
   `401` that sends them to sign in.
3. **Role.** The table above. `403`.
4. **A fresh check.** For the changes listed under
   [Confirm it's you](#confirm-its-you). `403` with `step_up`.
5. **Entitlement.** On a collection with a policy, the person's principal goes
   with the query and the policy decides document by document. A chunk that
   is not theirs and a chunk that does not exist get the same `404`.

The session cookie holds a random id, not a token: the session itself is kept
in the sign-in store under a hash of that id. A sign-in always gets a new id,
and signing out deletes it. Over https the cookie is `__Host-vx_sid`, which a
browser accepts only from this host, over https and for the whole site, so a
neighbouring subdomain can neither plant nor replace it. It is `HttpOnly` and
`Secure`, and `SameSite=Strict`, so no other site can send a request that
carries it. With single sign-on on it is `Lax`, because the provider's redirect
back is such a request; `VECTRIXDB_COOKIE_SAMESITE=strict` or `lax` decides
instead. The forgery token travels in `__Host-vx_csrf` the same way. On
`http://localhost` the two are `vx_sid` and `vx_csrf`, since the prefix needs
https.

With sign-in on, cross-origin requests are refused unless you name the origins
in `VECTRIXDB_CORS_ORIGINS`, and `*` is refused when the server starts, since
any site at all could then call the API with a signed-in person's cookie. The
dashboard is same-origin and needs none.

## What every reply carries

| Header | What it holds the browser to |
| --- | --- |
| `Content-Security-Policy` | The dashboard loads its own files, its fonts and its one chart library, runs no inline script, connects only back to this server, and may not be framed, send a form elsewhere or change its base address. A reply that is not a page may do nothing at all. |
| `Strict-Transport-Security` | Over https: a year, subdomains included. |
| `X-Content-Type-Options` | `nosniff`: a reply is what it says it is. |
| `X-Frame-Options` | `DENY`, for browsers older than `frame-ancestors`. |
| `Referrer-Policy` | `no-referrer`: an address with a collection's name in it stays here. |
| `Permissions-Policy` | No camera, microphone, location, payment or USB. |
| `Cross-Origin-Opener-Policy` | A page opened from the dashboard keeps no handle on it. |
| `Cache-Control` | `no-store` on the API and sign-in replies, which carry chunks and codes. |

The server does not say which web server it runs on. To show the dashboard
inside your own portal, name the portal:

```bash
export VECTRIXDB_FRAME_ANCESTORS="https://portal.example.com"
```

Name https origins, separated by spaces. `X-Frame-Options` is then left out,
because it cannot name one.

The dashboard runs no inline script, so its policy refuses all of it: script
that got into the page some other way, through a name or a document, is never
run. A button says what it does as data that one listener reads, and that
listener calls only the dashboard's own functions, by name, from a fixed list.
The only script from another host is the chart library, from
`cdnjs.cloudflare.com`, and it is pinned by its hash, so a copy that was
changed on the CDN is refused. A proxy that adds its own inline script to pages
will find it refused too. The API reference at `/docs` loads its viewer from a
CDN and keeps its own rules.
Behind a proxy that ends TLS, HSTS is sent when the proxy says
`X-Forwarded-Proto: https`, or when the public address is https.

## API keys

`VECTRIXDB_API_KEY` is an admin and `VECTRIXDB_READ_ONLY_API_KEY` reads and
does not search or write, as before. Give the server a key's SHA-256 in place
of the key, and its environment never holds a working key:

```bash
export VECTRIXDB_API_KEY_SHA256="$(printf %s "$KEY" | sha256sum | cut -d' ' -f1)"
export VECTRIXDB_READ_ONLY_API_KEY_SHA256="<sha-256 of the read-only key>"
```

**Keys for scripts.** An admin makes these under **API keys for scripts** in
the account menu: a name that says what uses the key, and what it may do, which
is read (`reader`), read and search (`searcher`), or read, search and write
(`operator`). The key starts `vx_`, is shown once, and is kept only as a hash.
Each one is revoked on its own, shows when it was last used, and is named in
the access log as `key:<name>`.

**A key can be narrower than a role.** Pick the collections it may reach, and
how long it works for. Both are on the same form, and over the API they are
`collections` and `expires_in_days`:

```bash
curl -s -X POST https://vectors.company.com/api/v1/keys \
  -H "api-key: $ADMIN_KEY" -H 'content-type: application/json' \
  -d '{"name": "handbook-bot", "role": "searcher",
       "collections": ["handbook"], "expires_in_days": 90}'
```

A key scoped to collections reaches those collections and the routes where the
server describes itself, and nothing else: another collection reads as one
that is not there, its listing holds only its own, and a route that cuts
across collections, like `/api/v1/documents` or `/api/v1/policies`, is
refused. The rule is written the closed way round, so a route added later is
refused to scoped keys until somebody decides otherwise. An expired key reads
as a key that is not ours, and the keys page marks it **Expired** rather than
leaving somebody to wonder why their app stopped.

Leave both out and the key is what a key has always been: everything its role
allows, until it is revoked.

**A key can have an allowance.** `requests_per_minute` on a key, or
`VECTRIXDB_KEY_REQUESTS_PER_MINUTE` for every key without a number of its own.
Past it the reply is 429 with `Retry-After`. The count is kept in the sign-in
store and not in the process, so three servers behind a load balancer give a
key one allowance and not three, where a restart used to forgive everybody.
The server's own `VECTRIXDB_API_KEY` is never limited.

**Keys from the server's console.** A deployment script, or a server whose
people all sign in with single sign-on and whose first key has to come from
somewhere:

```bash
vectrixdb keys add handbook-bot --collection handbook --days 90 --per-minute 120
vectrixdb keys list
vectrixdb keys revoke 9e621ba4
```

The key is printed once, to whoever is at that terminal, and the access log
records it as made by the server console.

**A key may arrive either way.** `api-key: <key>` is the header the dashboard
sends; `Authorization: Bearer <key>` is what a generated client, an HTTP
library and most gateways reach for. Both are read, and `api-key` wins when
both are sent.

A key is nobody in particular, so a collection with an entitlement policy
stays closed to it; serve those to people, or from your own service with
`as_principal`.

Writing the app that holds the key is
[Build an app on it](build-an-app.md).

## Tokens for apps

An app that signs people in with the same identity provider can act as the
person using it, by sending their access token as a Bearer token. Off until
the audience is named:

```bash
export VECTRIXDB_OIDC_API_AUDIENCE="api://vectrixdb"
```

Register that audience with the provider as an API of its own (in Entra ID,
**Expose an API**; in Okta or Auth0, an API or authorization server), and have
the app ask for a token for it. Each request is then decided the way a sign-in
is: the token is verified against the provider's published keys, the role
comes from `VECTRIXDB_OIDC_ROLE_MAP`, and the principal a policy reads comes
from `VECTRIXDB_OIDC_PRINCIPAL_CLAIMS`. So a collection with an entitlement
policy, closed to every key, answers the app with what that person may see,
and the access log names the person with the method `token`.

A token needs no place on the People list. The list is for the people who run
the platform; somebody using a collection through an app is judged by that
collection's policy. To give every app's token one role whoever it is for, and
no more than that, name it:

```bash
export VECTRIXDB_OIDC_TOKEN_ROLE=searcher
```

It is `reader`, `searcher`, `viewer` or `operator`, never `admin`, because a
token is an app. Unset, the token's groups decide, as they do at sign-in.

What is refused, each with a 401 and a line in the access log that never holds
the token: a token whose audience is anything else, which includes every
identity token, since those are issued to an application and would otherwise
let any app the person ever signed in to read their collections; another
issuer; an expired token; `alg: none`; a key the provider does not publish;
somebody in no mapped group, when no token role is set; and groups that did not fit in the token, because
looking them up needs a token for the directory and this one is for this
server. There is no session and no cookie, so no forgery token is asked for.
Changes that need a check from the last ten minutes are refused to a token,
because an app cannot give one.

## The access log

Every sign-in, failed sign-in, lockout, sign-out, read of content, search,
write and refusal, and every change to people, passkeys, authenticators,
recovery codes, passwords, keys and sharing, is one line of JSON in
`<database path>/auth/access.jsonl` (`VECTRIXDB_ACCESS_LOG` to move it), and
the Access page shows the newest.

```json
{"access_id": "acc_5f0c…", "at": 1789742000.1, "event": "search", "who": "ada@example.com", "role": "operator", "method": "oidc", "action": "search", "collection": "contracts", "route": "POST /api/v1/collections/contracts/text-search", "status": 200, "took_ms": 41.3}
{"access_id": "acc_91be…", "at": 1789742031.7, "event": "read", "who": "ada@example.com", "role": "operator", "method": "oidc", "action": "content.read", "collection": "contracts", "item": "acme-msa.md:4", "route": "GET /api/v1/collections/contracts/points/acme-msa.md:4"}
```

It names people, actions, collections, and the one chunk or document that was
opened, by id. It never holds a query, a chunk's
text or what a search returned, because a log that repeats what it guards is a
second copy of it with weaker rules. A read that cannot be recorded does not
happen: if the line cannot be written the request gets a `503`.

A search's line also says how long the server took over it, `took_ms`, and
what it answered, `status`; that is what the Overview's search time chart is
drawn from. It is the one line written after its request and not before,
because the time is only known then. The rule above holds all the same: the
reply has not left when the line is written, and if the line cannot be
written the reply is the `503` and the results never leave.

**On a platform that collects output.** App Service, Container Apps, ECS and
Kubernetes keep what a server writes to its output, and a file on the
container's disk is lost with the container. `VECTRIXDB_ACCESS_LOG=stdout`
writes each line there instead, marked so it can be told from everything else:

```json
{"log":"vectrixdb.access","access_id":"acc_dffb…","at":1789832156.5,"event":"search","who":"ada@example.com","role":"operator","method":"oidc","action":"search","collection":"contracts","route":"POST /api/v1/collections/contracts/text-search"}
```

The server then keeps no copy, so the Access page says the log is on the
server's output and to read it where the platform collects it.

For a collection with a policy, a signed-in person's search also writes the
full decision record to the audit trail, the same record the library writes,
when `VECTRIXDB_AUDIT_JSONL` is set. The query is fingerprinted with
`VECTRIXDB_AUDIT_QUERY_KEY`, or with a key derived from the sign-in secret if
that is unset.

## An open server stays on the machine

With no API key and no sign-in, `vectrixdb serve` listens on `127.0.0.1` and
refuses any other address:

```text
refusing to listen on 0.0.0.0 with no API key and no sign-in: anybody who can
reach the port could read every collection.
```

If a gateway in front of the server does the asking, say so with
`VECTRIXDB_ALLOW_OPEN=1`.

## Which version is running

With sign-in on, the version is shown to people who have signed in, beside the
"© 2026 VectrixDB" line in the sidebar, and left out of `/` and the OpenAPI
description for everybody else, so a scan of the port does not learn which
release to try. With sign-in off it is shown to all, as before. To put your
company's name and colours on the dashboard, see
[Put your company's name on it](branding.md).
