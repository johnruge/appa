"""The runner's form reading, filling, submit and safety checks, in a real (headless) browser,
against local pages built from the real Greenhouse / Lever / Ashby markup. No network."""

from __future__ import annotations

import pytest
from playwright.sync_api import Error as PlaywrightError

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

from appa.core.apply_fields import FormQuestion  # noqa: E402
from appa.runner import dom  # noqa: E402

COMBO_JS = """
for (const input of document.querySelectorAll('input[role=combobox]')) {
  const lb = document.getElementById(input.getAttribute('aria-controls'));
  const value = document.getElementById(input.id + '-value');
  const open = () => { lb.hidden = false; input.setAttribute('aria-expanded', 'true'); };
  input.addEventListener('click', open); input.addEventListener('focus', open);
  input.addEventListener('keydown', (e) => { if (e.key === 'Escape') lb.hidden = true; });
  input.addEventListener('input', () => { open(); for (const o of lb.children) o.hidden = !o.textContent.toLowerCase().includes(input.value.toLowerCase()); });
  for (const o of lb.children) o.addEventListener('click', () => { value.textContent = o.textContent; input.value = ''; input.dataset.chosen = o.textContent; lb.hidden = true; });
}
"""
SUBMIT_JS = """
document.getElementById('f').addEventListener('submit', (e) => {
  e.preventDefault();
  const missing = [...document.querySelectorAll('[required], [aria-required=true]')].filter((el) =>
    el.getAttribute('role') === 'combobox' ? !el.dataset.chosen : el.type === 'radio' ? !document.querySelector(`input[name="${el.name}"]:checked`) : !el.value);
  if (missing.length) { missing.forEach((el) => el.setAttribute('aria-invalid', 'true'));
    const err = document.createElement('div'); err.className = 'error'; err.textContent = 'This field is required'; document.body.append(err); return; }
  document.body.innerHTML = '<h1>Thank you for applying!</h1>';
});
"""

GREENHOUSE = f"""
<style>.visually-hidden{{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}}</style>
<ul role="listbox" style="position:absolute;left:-9999px"><li role="option">Afghanistan +93</li></ul>
<form id="f">
  <div class="field-wrapper"><label for="first_name">First Name<span>*</span></label><input id="first_name" type="text" required></div>
  <div class="field-wrapper"><label for="email">Email*</label><input id="email" type="text" aria-required="true"></div>
  <div class="select__container"><label id="q1-label" for="q1">Are you authorized to work in the US?*</label>
    <div class="select__control"><div id="q1-value"></div>
      <input id="q1" role="combobox" type="text" aria-labelledby="q1-label" aria-required="true" aria-controls="q1-listbox"></div>
    <div id="q1-listbox" role="listbox" hidden><div role="option">Yes</div><div role="option">No</div></div></div>
  <div><button type="button">Attach</button><label class="visually-hidden" for="resume">Attach</label>
    <input id="resume" type="file" class="visually-hidden"></div>
  <div class="field-wrapper"><label for="q2">Why Hooli?*</label><textarea id="q2" required></textarea></div>
  <input type="hidden" name="secret_token" value="x">
  <button type="submit">Submit application</button>
</form>
<script>{COMBO_JS}{SUBMIT_JS}</script>
"""

LEVER = f"""
<form id="f">
  <label><div class="application-label">Full name<span class="required">✱</span></div><div class="application-field"><input type="text" name="name" required></div></label>
  <li class="application-question"><div class="application-label"><div class="text">Will you now or in the future require sponsorship?<span class="required">✱</span></div></div>
    <ul><li><label><input type="radio" name="cards[a][field0]" value="Yes" required><span>Yes</span></label></li>
        <li><label><input type="radio" name="cards[a][field0]" value="No" required><span>No</span></label></li></ul></li>
  <li class="application-question"><div class="application-label"><div class="text">Language Skill(s)</div></div>
    <label><input type="checkbox" name="cards[b][field0]" value="English (ENG)"><span>English (ENG)</span></label>
    <label><input type="checkbox" name="cards[b][field0]" value="French (FRA)"><span>French (FRA)</span></label>
    <label><input type="checkbox" name="cards[b][field0]" value="German (DEU)"><span>German (DEU)</span></label></li>
  <li class="application-question"><div class="application-label"><div class="text">How did you hear about us?</div></div>
    <select name="cards[c][field0]"><option value="">Select...</option><option value="LinkedIn">LinkedIn</option><option value="Other">Other</option></select></li>
  <button type="submit">Submit application</button>
</form>
<script>{SUBMIT_JS}</script>
"""

ASHBY = """
<div class="ashby-application-form-field-entry"><label class="_required_x ashby-application-form-question-title" for="yn1">Do you have 7 years of experience?</label>
  <div class="ashby-application-form-input-yesno"><button type="button" aria-pressed="false">Yes</button><button type="button" aria-pressed="false">No</button>
    <input type="checkbox" tabindex="-1" name="yn1" style="display:none"></div></div>
<div class="ashby-application-form-field-entry"><label class="ashby-application-form-question-title" for="_systemfield_name">Legal Name</label>
  <div><input id="_systemfield_name" name="_systemfield_name" type="text" required></div></div>
<div class="ashby-application-form-field-entry"><label class="_required_y ashby-application-form-question-title" for="_systemfield_location">Where do you plan on working from?</label>
  <div><input role="combobox" aria-autocomplete="list" placeholder="Start typing..." class="loc"></div>
  <div class="opts" hidden><div role="option">Chicago, Illinois, United States</div><div role="option">Chicago Heights, Illinois</div></div></div>
<script>
const loc = document.querySelector('.loc'), opts = document.querySelector('.opts');
loc.addEventListener('input', () => { opts.hidden = !loc.value; });
for (const o of opts.children) o.addEventListener('click', () => { loc.value = o.textContent; loc.dataset.chosen = o.textContent; opts.hidden = true; });
for (const b of document.querySelectorAll('.ashby-application-form-input-yesno button'))
  b.addEventListener('click', () => { for (const x of b.parentElement.querySelectorAll('button')) x.setAttribute('aria-pressed', String(x === b)); });
</script>
"""


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            try:
                b = p.chromium.launch(headless=True)
            except Exception:
                pytest.skip("no Chrome/Chromium available for browser tests")
        yield b
        b.close()


@pytest.fixture()
def page(browser, monkeypatch):
    monkeypatch.setattr(dom, "pause", lambda *a, **k: None)
    pg = browser.new_page()
    pg.route("**/*", lambda r: r.abort())  # offline: nothing leaves the machine
    yield pg
    pg.close()


def by_ref(qs):
    return {q["ref"]: q for q in qs}


def test_greenhouse_read_fill_submit(page, tmp_path):
    page.set_content(GREENHOUSE)
    qs = by_ref(dom.extract(page))
    assert set(qs) == {"id:first_name", "id:email", "id:q1", "id:resume", "id:q2"}  # no hidden/decoy inputs
    assert qs["id:first_name"]["label"] == "First Name" and qs["id:first_name"]["required"]
    assert qs["id:q1"]["kind"] == "combobox" and qs["id:q1"]["options"] == ["Yes", "No"]  # not the decoy list
    assert qs["id:resume"]["kind"] == "file" and qs["id:resume"]["label"] == ""
    assert qs["id:q2"]["kind"] == "textarea"
    [FormQuestion(**q) for q in qs.values()]  # matches the server's schema

    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    plan = [
        {"ref": "id:first_name", "label": "First Name", "kind": "text", "value": "Pat"},
        {"ref": "id:email", "label": "Email", "kind": "text", "value": "pat@example.com"},
        {"ref": "id:q1", "label": "Authorized?", "kind": "combobox", "value": "Yes"},
        {"ref": "id:resume", "label": "", "kind": "file", "value": "@resume"},
        {"ref": "id:q2", "label": "Why Hooli?", "kind": "textarea", "value": "I like search."},
    ]
    report = dom.fill(page, plan, dom.Files(resume=str(resume)))
    assert all(r["ok"] for r in report), report
    assert page.locator("#q1-value").inner_text() == "Yes"
    assert page.evaluate("document.getElementById('resume').files.length") == 1
    assert not dom.captcha_visible(page)
    before = page.url
    dom.submit(page)
    assert dom.confirmation(page, before, timeout_s=5) == (True, [])


def test_validation_errors_are_reported_not_confirmed(page):
    page.set_content(GREENHOUSE)
    before = page.url
    dom.submit(page)
    confirmed, errors = dom.confirmation(page, before, timeout_s=2)
    assert not confirmed and any("required" in e.lower() for e in errors)  # native validation, not in the DOM


def test_lever_groups_and_select(page):
    page.set_content(LEVER)
    qs = by_ref(dom.extract(page))
    assert qs["name:name"]["label"] == "Full name"
    radio = qs["name:cards[a][field0]"]
    assert radio["kind"] == "radio" and radio["label"].startswith("Will you now or in the future require sponsorship")
    assert radio["options"] == ["Yes", "No"] and radio["required"]
    group = qs["name:cards[b][field0]"]
    assert group["kind"] == "checkbox_group" and len(group["options"]) == 3
    assert qs["name:cards[c][field0]"]["options"] == ["LinkedIn", "Other"]
    plan = [
        {"ref": "name:name", "label": "Full name", "kind": "text", "value": "Pat Doe"},
        {"ref": "name:cards[a][field0]", "label": "Sponsorship", "kind": "radio", "value": "Yes"},
        {
            "ref": "name:cards[b][field0]",
            "label": "Languages",
            "kind": "checkbox_group",
            "value": ["English (ENG)", "French (FRA)"],
        },
        {"ref": "name:cards[c][field0]", "label": "How heard", "kind": "select", "value": "LinkedIn"},
    ]
    assert all(r["ok"] for r in dom.fill(page, plan, dom.Files()))
    assert page.evaluate(
        "[...document.querySelectorAll('input[name=\"cards[b][field0]\"]:checked')].map(e => e.value)"
    ) == ["English (ENG)", "French (FRA)"]
    assert page.locator("select").input_value() == "LinkedIn"


def test_ashby_yes_no_buttons(page):
    page.set_content(ASHBY)
    qs = by_ref(dom.extract(page))
    yn = qs["name:yn1"]
    assert yn["kind"] == "yesno" and yn["options"] == ["Yes", "No"] and yn["required"]
    assert qs["id:_systemfield_name"]["label"] == "Legal Name"
    loc = qs["label:Where do you plan on working from?"]  # no id or name: found by its label
    assert loc["kind"] == "autocomplete" and loc["required"]
    report = dom.fill(
        page,
        [
            {"ref": "name:yn1", "label": "7 years?", "kind": "yesno", "value": "No"},
            {"ref": loc["ref"], "label": loc["label"], "kind": "autocomplete", "value": "Chicago"},
        ],
        dom.Files(),
    )
    assert all(r["ok"] for r in report), report
    assert page.locator(".loc").get_attribute("data-chosen") == "Chicago, Illinois, United States"
    assert page.get_by_role("button", name="No").get_attribute("aria-pressed") == "true"


def test_fill_reports_problems_instead_of_guessing(page):
    page.set_content(GREENHOUSE)
    report = dom.fill(
        page,
        [
            {"ref": "id:q1", "label": "Authorized?", "kind": "combobox", "value": "Maybe"},
            {"ref": "id:resume", "label": "", "kind": "file", "value": "@resume"},
            {"ref": "id:nope", "label": "Missing", "kind": "text", "value": "x"},
        ],
        dom.Files(),
    )
    assert [r["ok"] for r in report] == [False, False, False]
    assert "no option matching" in report[0]["error"] and "no file configured" in report[1]["error"]


def test_visible_captcha_challenge_is_detected(page):
    page.set_content(
        '<iframe src="https://www.google.com/recaptcha/api2/bframe?k=x" style="width:400px;height:500px"></iframe>'
    )
    assert dom.captcha_visible(page)
    page.set_content(
        '<iframe src="https://www.google.com/recaptcha/api2/bframe?k=x" style="width:400px;height:500px;visibility:hidden"></iframe>'
    )
    assert not dom.captcha_visible(page)


def test_browser_refuses_private_addresses_and_pins_navigation(browser):
    from appa.runner.browser import Browser, NotPublic, site_of

    b = Browser(headless=True)
    for url in (
        "http://192.168.1.1/",
        "http://localhost:8787/",
        "http://169.254.169.254/latest/",
        "file:///etc/passwd",
    ):
        with pytest.raises(NotPublic):
            b.open(url)  # refused before any browser work
    assert site_of("careers.acme.com") == "acme.com" and site_of("jobs.acme.co.uk") == "acme.co.uk"
    b.ctx = browser.new_context()
    b.ctx.route("**/*", b._guard)
    b.check_ips = False
    b.allowed, b.allowed_sites = {"careers.acme.example"}, {"acme.example", "workable.com"}
    assert b.allows("jobs.acme.example") and b.allows("apply.workable.com") and not b.allows("evil.example")
    page = b.ctx.new_page()
    with pytest.raises(PlaywrightError):
        page.goto("https://evil.example/steal", timeout=5000)
    assert any("evil.example" in u for u in b.blocked)
    b.ctx.close()


def test_popups_are_closed_but_our_own_pages_are_not(browser):
    from appa.runner.browser import Browser

    b = Browser(headless=True)
    b.ctx = browser.new_context()
    b.ctx.on("page", b._no_popups)
    page = b.ctx.new_page()
    page.route("**/*", lambda r: r.abort())
    page.set_content("<a href='about:blank' target='_blank' id='x'>open</a>")
    page.click("#x")
    page.wait_for_timeout(500)
    assert not page.is_closed()
    assert [p for p in b.ctx.pages if not p.is_closed()] == [page]
    b.ctx.close()


def test_email_verification_step_is_detected(page):
    page.set_content(
        "<p>A verification code was sent to pat@example.com. To submit your application, "
        "enter the 8-character code to confirm you're a human.</p><input aria-label='Security code'>"
    )
    assert dom.verification_requested(page)
    confirmed, _ = dom.confirmation(page, page.url, timeout_s=1)
    assert not confirmed  # waiting for a code is not a submitted application
    page.set_content("<h1>Thank you for applying!</h1>")
    assert not dom.verification_requested(page)


# ---- any site: fake company sites served locally ---------------------------------------------

GENERIC_FORM = """
<form id="f" novalidate>
  <div class="form-group"><label>First name *</label><input name="fname" required></div>
  <div class="form-group"><label>Last name *</label><input name="lname" required></div>
  <div class="form-group"><label for="em">Email address</label><input id="em" type="email" required></div>
  <div class="form-field"><span class="field-label">Will you now or in the future require sponsorship?</span>
    <select name="spons"><option value="">Choose…</option><option>Yes</option><option>No</option></select></div>
  <div class="form-group"><label>Resume/CV</label><input type="file" name="cv"></div>
  <div class="question"><div class="question-title">Where did you hear about us?</div><input class="free"></div>
  <button type="submit">Submit application</button>
</form>
<script>
document.getElementById('f').addEventListener('submit', (e) => { e.preventDefault();
  if (![...document.querySelectorAll('[required]')].every((x) => x.value)) { document.body.insertAdjacentHTML('beforeend', '<div class="error">Required</div>'); return; }
  document.body.innerHTML = '<h1>Thanks for applying!</h1>'; });
</script>
"""
SITES = {
    "https://careers.acme.example/jobs/42": "<h1>Software Engineer</h1><p>Build things.</p><a href='/jobs/42/apply'>Apply now</a>",
    "https://careers.acme.example/jobs/42/apply": GENERIC_FORM,
    "https://careers.acme.example/embedded": "<h1>Join us</h1><iframe src='https://apply.workable.com/acme/j/42/apply' style='width:800px;height:900px'></iframe>",
    "https://apply.workable.com/acme/j/42/apply": GENERIC_FORM,
    "https://careers.acme.example/login": "<h1>Sign in to apply</h1><input type=email name=e><input type=password name=p>",
    "https://careers.acme.example/wizard": "<form><div class='form-group'><label>First name</label><input name=a></div>"
    "<div class='form-group'><label>Last name</label><input name=b></div><button>Next</button></form>",
    "https://careers.acme.example/offsite": "<h1>SWE</h1><a href='https://jobs.elsewhere.example/apply/42'>Apply</a>",
    "https://careers.acme.example/nothing": "<h1>We're hiring</h1><p>Email us.</p>",
    "https://careers.acme.example/botframe": "<iframe src='https://geo.captcha-delivery.com/captcha/?x=1' style='width:600px;height:400px'></iframe>",
    "https://geo.captcha-delivery.com/captcha": "<p>Access is temporarily restricted</p>",
    "https://careers.acme.example/botwall": "<h1>Access is temporarily restricted</h1><p>We detected unusual activity from your device or network.</p>",
}


@pytest.fixture()
def site(browser, monkeypatch):
    from appa.runner.browser import ATS_DOMAINS, ATS_HOSTS, Browser, site_of

    monkeypatch.setattr(dom, "pause", lambda *a, **k: None)
    b = Browser(headless=True)
    b.check_ips = False
    b.ctx = browser.new_context()

    def serve(route):
        req = route.request
        from urllib.parse import urlparse

        host = urlparse(req.url).hostname or ""
        if req.is_navigation_request() and req.frame.parent_frame is None and not b.allows(host):
            b.blocked.append(req.url)
            return route.abort()
        body = SITES.get(req.url.split("?")[0].rstrip("/"))
        return route.fulfill(body=body, content_type="text/html") if body else route.abort()

    b.ctx.route("**/*", serve)

    def open_(url):
        host = url.split("/")[2]
        b.allowed, b.allowed_sites, b.blocked = set(ATS_HOSTS) | {host}, {site_of(host), *ATS_DOMAINS}, []
        page = b.ctx.new_page()
        page.goto(url)
        return page

    yield b, open_
    b.ctx.close()


def test_any_site_follows_apply_and_fills_a_generic_form(site, tmp_path):
    b, open_ = site
    page = dom.find_form(open_("https://careers.acme.example/jobs/42"), b)
    assert page.url.endswith("/jobs/42/apply")
    qs = {q["label"]: q for q in dom.extract(page)}
    assert {
        "First name",
        "Last name",
        "Email address",
        "Resume/CV",
        "Where did you hear about us?",
        "Will you now or in the future require sponsorship?",
    } <= set(qs)
    assert qs["Will you now or in the future require sponsorship?"]["options"] == ["Yes", "No"]
    assert qs["Where did you hear about us?"]["ref"].startswith("label:")  # no id or name
    from appa.apply.planner import build_plan
    from appa.core.apply_fields import FormQuestion

    bank = {
        "first_name": "Pat",
        "last_name": "Doe",
        "email": "pat@example.com",
        "requires_sponsorship": "Yes",
        "how_heard": "LinkedIn",
    }
    plan = build_plan([FormQuestion(**q) for q in qs.values()], bank)
    assert plan.ready, plan.missing
    resume = tmp_path / "cv.pdf"
    resume.write_bytes(b"%PDF-1.4")
    items = [i.model_dump() for i in plan.items]
    report = dom.fill(page, items, dom.Files(resume=str(resume)))
    assert all(r["ok"] for r in report), report
    before = page.url
    dom.submit(page)
    assert dom.confirmation(page, before, timeout_s=5)[0]


def test_any_site_opens_an_embedded_form(site):
    b, open_ = site
    page = dom.find_form(open_("https://careers.acme.example/embedded"), b)
    assert page.url == "https://apply.workable.com/acme/j/42/apply"
    assert len(dom.extract(page)) >= 5


@pytest.mark.parametrize(
    "url,reason",
    [
        ("https://careers.acme.example/login", "login"),
        ("https://careers.acme.example/wizard", "multi-page"),
        ("https://careers.acme.example/offsite", "jobs.elsewhere.example"),
        ("https://careers.acme.example/nothing", "couldn't find an application form"),
        ("https://careers.acme.example/botwall", "bot protection"),  # handed back, never evaded
        ("https://careers.acme.example/botframe", "bot protection"),  # wall inside a frame (DataDome)
    ],
)
def test_any_site_hands_back_what_it_cant_do(site, url, reason):
    b, open_ = site
    with pytest.raises(dom.Blocked, match=reason):
        dom.find_form(open_(url), b)
