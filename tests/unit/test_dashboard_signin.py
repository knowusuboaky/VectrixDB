"""The dashboard's side of sign-in, checked the only way a suite with no browser can: by structure.

The flows themselves were walked in a browser: the sign-in card, the emailed
link, the QR code, the recovery codes, the signed-in shell and the Access
page. What runs here keeps the load-bearing parts from being edited away.
"""

from __future__ import annotations

import re
from pathlib import Path

DASH = Path(__file__).resolve().parents[2] / "vectrixdb" / "dashboard"
JS = (DASH / "app.js").read_text(encoding="utf-8")
EVAL = (DASH / "evaluate.js").read_text(encoding="utf-8")
CHUNK = (DASH / "chunking.js").read_text(encoding="utf-8")
HTML = (DASH / "index.html").read_text(encoding="utf-8")
CSS = (DASH / "app.css").read_text(encoding="utf-8")


def test_the_first_thing_the_page_asks_is_who_is_here():
    boot = JS[JS.index("async function boot()") :]
    assert boot.index("await whoAmI()") < boot.index("await loadAuth()") < boot.index("connectWs()")
    assert "if (!(await whoAmI())) return;" in boot, "nothing else is fetched for somebody who is not signed in"


def test_while_the_sign_in_card_is_up_the_application_is_absent_not_covered():
    assert "document.querySelector('.app').hidden = true" in JS
    assert "[hidden] { display: none !important; }" in CSS, "or a display rule brings the application back"


def test_a_change_carries_the_forgery_token_from_the_cookie_script_may_read():
    assert "cookie('__Host-vx_csrf') || cookie('vx_csrf')" in JS and "h['X-CSRF-Token'] = token" in JS


def test_a_401_anywhere_brings_up_sign_in():
    assert "res.status === 401 && body && body.data && body.data.signin" in JS


def test_the_emailed_link_does_nothing_by_being_opened():
    enrol = JS[JS.index("function gateEnrol(token)") : JS.index("async function gateEnrolBegin")]
    assert "api(" not in enrol, "a mail scanner opens every link; the token is spent by a button"
    assert "on('click', ['gateEnrolBegin'" in enrol


def test_a_password_field_only_where_the_server_turned_passwords_on():
    gate = JS[JS.index("/* ------------------------------------------------------------- sign-in */") : JS.index("/* -------------------------------------------------------------- access */")]
    assert gate.count('type="password"') == 3, "one field made in one place, the emergency page's own, and the step-up's for Developer Access"
    field = gate[gate.index("function passwordField(") :][:400]
    assert 'type="password"' in field
    glass = gate[gate.index("async function gateBreakGlass(") : gate.index("async function breakGlassSignIn(")]
    assert 'type="password" id="glass-password"' in glass and "one-time-code" not in glass and "glass-code" not in JS, "no code is asked for"
    # Asked for on sign-in only when the server says passwords are on, and never without the code.
    assert "if (m.email && m.email.passwords)" in gate and "d.passwords ? passwordField(" in gate
    assert 'autocomplete="one-time-code"' in gate


def test_the_username_form_is_gone_and_the_list_signs_in_by_email():
    """With passwords on and no single sign-on, the list's own way is the email form, then the password and the code together."""
    assert "gateLocal" not in JS and "forgotFromLocal" not in JS and "gate-user" not in JS
    assert "if (m.email && m.email.passwords)" in JS, "the email form still asks for both where the server says so"


def test_developer_access_is_drawn_as_the_canvas_has_it():
    """Username and password, the This machine only pill, and Back to sign in when there is another way."""
    dev = JS[JS.index("function gateDeveloper()") : JS.index("async function developerSignIn(")]
    assert "<h1>Developer Access</h1>" in dev
    assert "Local role testing is available only on this machine. Use the developer credentials in its settings." in dev
    assert 'label class="field">Username<input id="dev-user" autocomplete="username" required placeholder="admin.user"' in dev
    assert "passwordField('Password', 'current-password', \"From this machine's settings\")" in dev
    assert "This machine only" in dev and "plain: true" in dev and "label: 'Developer Access'" in dev
    assert "m.oidc || m.email ?" in dev and "Back to sign in" in dev
    signin = JS[JS.index("async function developerSignIn(") : JS.index("function leaveDeveloper()")]
    assert "api('/auth/developer'" in signin and "username: $('dev-user').value.trim(), password: $('gate-password').value" in signin


def test_nothing_in_the_sign_in_box_names_developer_access():
    dialog = JS[JS.index("function showGate()") : JS.index("/* Developer Access: accounts named")]
    assert "Developer Access" not in dialog.split("gateBox(")[1], "the single sign-on button is how it is reached"
    assert "${on('click', ['pressSso'])}" in dialog


def test_the_button_is_pressed_and_on_this_machine_the_spinner_finds_it():
    press = JS[JS.index("function pressSso()") : JS.index("function localDetected()")]
    assert "if (m.developer) return localDetected();" in press
    found = JS[JS.index("function localDetected()") : JS.index("function ssoNotSetUp()")]
    assert "Local development detected${DOTS}" in found and "Preparing developer sign-in options for this machine." in found
    assert "setTimeout(m.oidc && !m.oidc.pending ? localAccess : ssoNotSetUp, 900)" in found


def test_with_no_single_sign_on_the_spinner_says_so_then_opens_developer_access():
    spin = JS[JS.index("function ssoNotSetUp()") : JS.index("function localAccess()")]
    assert "Single sign-on not configured${DOTS}" in spin and "Open it now" in spin
    assert "Single sign-on is unavailable for this local environment right now. Developer Access is available on localhost." in spin
    assert "setTimeout(gateDeveloper, 1100)" in spin
    assert "if (m.developer && !m.oidc && !m.email && !state.pastDeveloper) return localDetected();" in JS, "with nothing else to press, it starts by itself"
    assert "state.pastDeveloper = true" in JS, "back to sign in does not loop to the spinner"


def test_with_single_sign_on_set_up_this_machine_is_given_the_choice():
    choice = JS[JS.index("function localAccess()") : JS.index("function gateDeveloper()")]
    assert "<h1>Local development detected</h1>" in choice
    assert choice.index("['startSso']") < choice.index("['gateDeveloper']"), "single sign-on first, as the normal way in"
    assert "Single sign-on remains the normal sign-in path. Developer Access is limited to localhost and is not exposed in shared or deployed environments." in choice
    for action in ("pressSso", "checkSso", "openEmailWay"):
        assert f"'{action}'" in JS[JS.index("const ACTIONS = new Set([") :][:400]


def test_a_change_that_matters_asks_developer_access_for_its_password():
    step = JS[JS.index("function stepUp(ways)") : JS.index("function stepUpDone(")]
    assert "const password = ways.includes('password');" in step and 'id="stepup-password"' in step
    confirm = JS[JS.index("async function stepUpPassword(ev)") : JS.index("async function stepUpPasskey()")]
    assert "JSON.stringify({ password: $('stepup-password').value })" in confirm


def test_emergency_sign_in_is_its_own_page_that_nothing_links_to():
    assert "hash.startsWith('#/break-glass')" in JS and "location.hash.startsWith('#/break-glass')) gateBreakGlass()" in JS
    sign_in = JS[JS.index("function showGate()") : JS.index("/* Developer Access: accounts named")]
    assert "break-glass" not in sign_in.replace("|break-glass|developer)", ")"), "the usual sign-in never offers it"
    assert "#/break-glass" not in HTML
    glass = JS[JS.index("async function gateBreakGlass(") : JS.index("function leaveBreakGlass()")]
    assert "Emergency sign-in is off" in glass and "For when the usual sign-in is down. Every sign-in here is recorded." in glass
    assert "'/auth/break-glass'" in glass and "Use the usual sign-in" in glass
    assert "{ username: $('glass-user').value.trim(), password: $('glass-password').value }" in glass, "as the canvas draws it: a username and a password"


def test_admins_see_the_banner_while_emergency_sign_in_is_on():
    assert "breakGlass: d.break_glass || null" in JS
    banner = JS[JS.index("function renderGlassBanner()") : JS.index("async function openAbout()")]
    assert "Emergency sign-in is on until ${esc(glassTime(glass.until))}.</b> Turn it off when sign-in is back." in banner
    assert 'id="glass-banner" role="status" hidden' in HTML


def test_about_is_for_admins_with_the_version_the_licence_and_the_notice():
    assert "$('mi-about').hidden = state.signinOn ? !(me && can('about.read')) : false;" in JS
    about = JS[JS.index("async function openAbout()") : JS.index("async function signOut()")]
    assert "api('/api/v1/about')" in about and "d.licence_line" in about and "d.notice" in about and "/api/v1/about/licence" in about
    assert 'id="mi-about"' in HTML and 'id="dlg-about"' in HTML
    assert "renderGlassBanner();" in JS and "$('copyline').textContent = COPY;" in JS, "the sidebar line carries no version now"


def test_the_note_under_the_sign_in_card_names_only_the_ways_this_server_has():
    """A server with single sign-on alone has no authenticator app to mention, and one with the email list alone no company account."""
    foot = JS[JS.index("const foot = closed") : JS.index("gateBox(`<div class=\"stack\"><h1>Sign in</h1>")]
    assert "m.oidc && m.email && !m.email.passwords ? 'Your work account signs you in. An authenticator app you added works too.'" in foot, "no passkey beside single sign-on"
    assert "m.oidc ? \"Your work account signs you in. There's no password here.\"" in foot
    assert "\"There's no password here. Your authenticator app signs you in.\"" in foot


def test_a_refusal_at_the_provider_is_put_in_words_and_the_address_is_never_repeated():
    """The error arrives in the address, which anybody can write, so only a code on the list becomes words."""
    errors = JS[JS.index("const SIGNIN_ERRORS = {") : JS.index("const NO_ACCESS")]
    for code in ("provider_access_denied", "provider_temporarily_unavailable", "provider_server_error"):
        assert f"  {code}: () =>" in errors
    shown = JS[JS.index("function ssoError(code)") : JS.index("/* Passkeys.")]
    assert "${code" not in shown and "esc(code" not in shown and "${esc(say)}" in shown
    assert "The sign-in did not finish. Try again." not in shown, "the heading says that already"


def test_what_the_server_sends_is_escaped_except_the_qr_it_drew_itself():
    gate = JS[JS.index("async function gateEnrolBegin") : JS.index("async function gateEnrolConfirm")]
    raw = re.findall(r"\$\{(d\.[a-z_]+)\}", gate)
    assert raw == ["d.qr"], raw


def test_the_qr_code_brings_its_own_light_tile_and_its_own_size():
    """Dark on light is how the standard draws a code and how every scanner reads one, so no theme boxes it; a long address draws larger."""
    for where in (".gate", ".dialog"):
        box = re.search(rf"^{re.escape(where)} \.qr \{{([^}}]*)\}}", CSS, re.M).group(1)
        assert "background" not in box and "padding" not in box and "width: 200px" not in box, where
        assert re.search(rf"^{re.escape(where)} \.qr svg \{{ display: block; max-width: 100%; height: auto; \}}", CSS, re.M), where


def test_a_page_somebody_may_not_use_is_out_of_the_menu_and_its_address_goes_home():
    assert "const NEEDS = { search: 'search', evaluate: 'evaluation.read', ingest: 'content.write', audit: 'audit.read', access: 'access.read', console: 'content.read' };" in JS
    assert "(NEEDS[page] && !can(NEEDS[page]))) page = 'overview'" in JS
    assert 'data-page="access"' in HTML and 'id="page-access"' in HTML


def test_a_viewer_is_told_why_there_is_no_text():
    assert "Your role shows that chunks exist, not what they say." in JS


class TestTextIsShownWhenAskedFor:
    def test_the_points_table_is_built_from_a_listing_with_no_text_in_it(self):
        points = JS[JS.index("async function loadPoints()") : JS.index("function revealButton(")]
        assert "&index=true" in points
        assert "/points/${" not in points, "the table used to fetch every chunk on the page to fill a column"
        assert "<span>Text</span>" not in points

    def test_one_control_everywhere_a_chunk_is_listed(self):
        assert JS.count("function revealButton(") == 1 and JS.count("async function toggleChunk(") == 1
        assert JS.count("revealButton(") >= 5, "points, quality, provenance, search, and its own definition"
        button = JS[JS.index("function revealButton(") : JS.index("async function toggleChunk(")]
        assert 'aria-expanded="false"' in button and "Show text" in button

    def test_only_somebody_who_may_read_is_offered_it(self):
        for place in ("reads ? revealButton(", "can('content.read') ? revealButton(state.collection, w.id)", "can('content.read') ? revealButton(collection, r.id)"):
            assert place in JS, place

    def test_provenance_and_quality_ask_for_no_text(self):
        assert "/provenance/${encodeURIComponent(id)}?text=false" in JS and "/quality?text=false" in JS
        assert "esc(w.text)" not in JS and "esc(d.text)" not in JS

    def test_search_asks_for_excerpts(self):
        assert "const excerpt = '?snippet=200';" in JS and "${at(path)}${excerpt}" in JS
        assert "r.snipped && !redacted && can('content.read')" in JS, "a chunk already shown whole needs no button"

    def test_the_quality_card_does_not_call_a_clean_collection_bad(self):
        assert "Worst chunks" not in JS
        assert "No chunk is below the line." in JS and "w.below_line" in JS and "w.reasons" in JS

    def test_a_whole_document_is_offered_only_to_somebody_who_may_open_one(self):
        chunk = JS[JS.index("async function chunkHtml(") :][:1600]
        assert "can('document.read')" in chunk and "['openDocument'" in chunk

    def test_an_admin_gives_it_to_an_operator_by_name(self):
        assert "Whole documents" in JS and "['document.read']" in JS
        assert "grants === undefined ? { email, role } : { email, role, grants }" in JS, "changing a role must not quietly take a grant away"


def test_destructive_things_ask_in_a_dialog_not_a_browser_prompt():
    access = JS[JS.index("/* -------------------------------------------------------------- access */") : JS.index("/* ------------------------------------------------------------ settings */")]
    assert "prompt(" not in access and "confirm(" not in access.replace("sure(", "")
    assert 'id="dlg-person"' in HTML


def test_the_new_tables_are_classes_with_rules_like_every_other():
    for name in ("t-people", "t-grants", "t-access"):
        assert f'class="tr head {name}"' in JS and re.search(rf"^\.{name} \{{", CSS, re.M)


def test_the_copy_has_no_dashes():
    for source in (JS, EVAL, CHUNK, HTML):
        assert "—" not in source


def test_every_function_the_page_calls_is_one_it_defines():
    """A collection's Overview called mode2, which nothing defined, so it threw before it drew anything."""
    pages = JS + "".join((DASH / name).read_text(encoding="utf-8") for name in ("trends.js", "evaluate.js", "chunking.js"))
    defined = set(re.findall(r"(?:function\s+|const\s+|let\s+|var\s+)([A-Za-z_$][\w$]*)", pages))
    # Calls written inside the page's own templates, which is where one goes unnoticed until the page is opened.
    called = set(re.findall(r"\$\{([a-z][A-Za-z0-9_]*)\(", pages))
    browsers_own = {"encodeURIComponent", "decodeURIComponent", "parseInt", "parseFloat"}
    assert not sorted(called - defined - browsers_own), sorted(called - defined - browsers_own)


def test_a_tab_the_policy_refuses_says_so_and_is_never_left_blank():
    tabs = JS[JS.index("async function loadCollectionTab()") : JS.index("/* The chunks below the quality line")]
    assert "try { await loadPoints(); } catch (e) { tabRefused('cpanel-points', name, e); }" in tabs
    assert "} catch (e) { tabRefused('cpanel-quality', name, e); }" in tabs
    refused = JS[JS.index("function tabRefused(") : JS.index("async function loadCollectionTab()")]
    assert "restrictedResults(name, data)" in refused and "e.status === 403" in refused


def test_the_dots_in_a_spinner_heading_do_not_push_its_words_off_centre():
    assert ".sso-screen h1 .dots { position: absolute; min-width: 0; }" in CSS


def test_a_guest_is_not_asked_to_sign_in_by_a_chart_they_did_not_ask_for():
    """A guest opening a collection had the sign-in box come up by itself: the growth chart was refused, and a refusal opens it."""
    assert "if (!state.guest) api(`/api/v1/collections/${enc}/growth?days=30`, { quiet: true })" in JS


def test_a_collection_with_a_policy_is_counted_the_same_on_both_pages():
    has = JS[JS.index("function hasPolicy(name)") : JS.index("function stateOf(name)")]
    assert "pol.gated" in has and "v.method" in has and "stateOf(name).text === 'Policied'" in has
    assert "policied: state.collections.filter((c) => hasPolicy(c.name)).length" in JS
    assert "if (collectionFilter === 'policied' && !hasPolicy(c.name)) return false;" in JS
    assert "await Promise.all([loadCollectionList(), loadModels(), loadPolicies()]);" in JS, "the Collections page reads the policies too"


def test_what_is_in_a_shared_store_is_not_given_this_disks_size_or_this_processs_start():
    assert JS.count("metric('Kept in', 'Shared store', 'read by every instance')") == 2, "the server's tile and a collection's"
    assert "${c.created_at ? `<div class=\"tr t-split\"><span>Created</span>" in JS


def test_a_server_with_collections_of_its_own_is_not_asked_to_take_the_demo():
    learn = JS[JS.index("function learnForThisServer()") : JS.index("function gotoGuide(")]
    assert "state.collections.length > 0 && !state.collections.some((c) => c.name === 'demo')" in learn, "an empty server, and one with the demo, keep it"
    assert "$('demo-strip').hidden = own; $('tut-quick').hidden = own;" in learn
    assert "q.className = 'try-q'" in learn and "b.replaceWith(q)" in learn, "each example becomes its words, not a button"
    assert "q.replaceWith(b)" in learn, "and the button comes back once the demo is there to run it against"
    assert 'id="demo-strip"' in HTML and 'id="tut-quick"' in HTML
    assert ".tries.said {" in CSS and ".try-q {" in CSS and ".try.said" not in CSS
    show = JS[JS.index("function showLearn(") : JS.index("function learnForThisServer()")]
    assert "if (!state.collectionsRead) loadCollectionList().then(learnForThisServer)" in show, "opened by its address, it reads the collections first"
    assert "const own = !state.collectionsRead ||" in learn, "until the list is read, nothing is drawn that may be taken away again"
    listed = JS[JS.index("async function loadCollectionList()") : JS.index("async function loadModels()")]
    assert "state.collectionsRead = true;" in listed
