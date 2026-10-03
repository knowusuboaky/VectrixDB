# Put your company's name on it

Before a deployment, a company can give the dashboard its own name, logo,
colours and line. Settings read when the server starts:

```bash
export VECTRIXDB_BRAND_NAME="Harbour Labs"
export VECTRIXDB_BRAND_LOGO=/etc/vectrixdb/logo.svg
export VECTRIXDB_BRAND_LOGO_DARK=/etc/vectrixdb/logo-dark.svg   # optional
export VECTRIXDB_BRAND_ACCENT="#0f766e"
export VECTRIXDB_BRAND_WORDMARK=on                              # optional
export VECTRIXDB_BRAND_COPYRIGHT="Harbour Labs"                 # optional
export VECTRIXDB_BRAND_PALETTE=/etc/vectrixdb/palette.json      # optional
vectrixdb serve --host 0.0.0.0
```

The start-up message says what it found:

```text
  Brand: Harbour Labs as a wordmark, logo.svg and logo-dark.svg, accent #0f766e, a palette for dark and light, © 2026 Harbour Labs
```

## What changes, and what does not

The name sits beside the logo at the top of the sidebar and on the sign-in
page, and it is the browser tab's title, the name in sign-in emails and the
account name an authenticator app shows. The logo replaces the mark in the
sidebar, on the sign-in page, in the browser tab and in the middle of the QR
code an authenticator app scans. The accent colours the buttons, the
selected item and the links.

The page is sent with the brand already in it, so nobody sees VectrixDB's name
and colour flash first, and a browser that had the page before the brand was
set is sent the new one.

The line under the sidebar says whose the dashboard is: **© 2026 VectrixDB**
unless `VECTRIXDB_BRAND_COPYRIGHT` names someone else, as in **© 2026 Harbour
Labs**. It carries no version: a version on a page anybody can open tells an
attacker which fixes are missing.

The API, the command line, the logs and the documentation still say VectrixDB.
On a branded dashboard nobody who has not signed in sees the name VectrixDB.
An admin finds it under **About** in the account menu, with the version, the
Apache licence in a line, the NOTICE file whole and a link to the licence
itself. Apache 2.0 asks that the licence and the notice travel with the work:
they are in the package, and on that page. `GET /api/v1/about` answers an
admin with the same, and `GET /api/v1/about/licence` with the licence in plain
text.

## The logo

- SVG, PNG or JPEG. It is recognised by what the file holds, not by its name.
- At most 512 KB, and a PNG or JPEG at least 64 pixels on its shorter side,
  since it is shown at 28 and a screen may double that.
- Square fits best. It sits in a rounded square in the sidebar and fills the
  browser tab. A logo that is not square works, with a warning at start-up.
- SVG or PNG looks cleanest. A JPEG cannot be transparent, so it shows as a
  tile.
- An SVG that could run anything is refused: a script, an event handler, a
  `foreignObject`, a `javascript:` address, or anything fetched from somewhere
  else. Every logo is served as an image, under a policy that lets it run
  nothing.
- `VECTRIXDB_BRAND_LOGO_DARK` is for a logo that disappears on the dark theme.
  It needs `VECTRIXDB_BRAND_LOGO` as well.

A host that keeps settings and no files, such as a function app, can give the
logo inside its setting as a `data:` address. The type it claims is not
trusted: the bytes are read like a file's and held to the same rules.

```bash
export VECTRIXDB_BRAND_LOGO="data:image/svg+xml;base64,PHN2ZyB4bWxucz0i..."
```

## The name as a wordmark

A company whose mark is a symbol beside its name, the name set in its own
colour, turns on `VECTRIXDB_BRAND_WORDMARK`. The name is then larger and in
the accent's text shade, beside the logo, in the sidebar and on every sign-in
page. Off, it is the plain name beside the mark, as before.

## The colour

One colour, written `#rrggbb`. The dashboard works out the rest for each
theme: the fill for buttons, the ink on the fill (black or white, whichever
reads better), a shade dark enough to read as text on the light theme and one
light enough on the dark theme, and a tint behind whatever is selected. So a
pale brand colour still gives readable links, and a dark one still gives
readable buttons.

An accent close to the red that means something is wrong is allowed, with a
warning at start-up, since plenty of companies are red.

## The palette

For a company whose own apps have a look of their own: the grounds, the inks
and the lines of each theme. The names say what each paints, and anything left
out stays VectrixDB's.

| Name | Paints |
| --- | --- |
| `page` | Behind everything |
| `sidebar` | The sidebar and code blocks |
| `card` | Cards and dialogs |
| `field` | Inside inputs. Left out, the sidebar's colour |
| `subtle` | Notices, tags and quiet fills |
| `text`, `text-2`, `muted` | Text, secondary text, and the faintest |
| `line`, `border` | Dividers, and the edges of controls |
| `success`, `warning`, `danger`, `info` | The states. Each keeps its meaning, so each must still read on a card |

```json
{
  "light": {"page": "#f4f6fa", "sidebar": "#ffffff", "card": "#ffffff", "field": "#eef2f8",
            "text": "#10213a", "text-2": "#354a66", "muted": "#4c5f7a", "line": "#e2e8f2", "border": "#d3dbe8"},
  "dark":  {"page": "#050a10", "sidebar": "#131a24", "card": "#131a24", "text": "#e9f0fb"}
}
```

`VECTRIXDB_BRAND_PALETTE` takes the JSON itself, a file that holds it, or a
`data:` address. Every ink is measured on the grounds it sits on, and one that
does not reach a contrast of 4.5 to 1 stops the server, with the pair named:

```text
VECTRIXDB_BRAND_PALETTE: in the light theme, muted #8fa0b6 on page #f7f5f1 is 2.5 to 1, and text needs 4.5 to 1 to be read. Darken or lighten one of them
```

The accent's text shade is worked out on the palette's own page and cards.

The QR code an authenticator app scans is drawn dark on light on either
theme: the light theme's `text` on its `page`, when the two are at least 7
to 1 apart. A palette whose two are closer, readable as text and faint to a
camera, gets VectrixDB's own ink and paper for the code.

## A dashboard of your own

A dashboard some other service sends, forwarding its calls to this server,
takes the brand from here: `GET /brand.json` says the name, the logos, the
accent, whether the name is a wordmark and the line, and `GET /brand.css` is
the colours as a stylesheet. The page's script writes the name, the line and
the logo in when it starts, from the data block the service fills in.

## When something is wrong

A mistake in any of these stops the server from starting, with a message that
names the setting and says what to change:

```text
VECTRIXDB_BRAND_ACCENT is 'teal'. Write it as #rrggbb, for example #0b5cad
```

A deployment that quietly came up with somebody else's brand would be worse
than one that did not come up. The name is at most 40 characters, since it
sits beside a logo in a narrow sidebar, and the line's holder at most 60. The
holder is only whose it is: the © and the year are added.

`GET /brand.json` returns what the page is told: the name, whether a brand is
set, where the logos are, the accent, whether the name is a wordmark, and the
line.
