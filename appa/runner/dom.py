"""Read application forms and fill them, in the page. Greenhouse, Lever and Ashby.

Questions get a stable `ref` from the control's own id or name, so a plan made on one
page load finds the same fields on the next (e.g. when you ask appa to fill it again).
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass

from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PWTimeout

EXTRACT_JS = r"""
() => {
  const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  const clean = (s) => s.replace(/[*✱]/g, '').replace(/\s+/g, ' ').trim().slice(0, 1500);
  const shown = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const GENERIC = /^(attach|upload( file)?|choose file|select\.\.\.|)$/i;
  const ref = (el) => (el.id ? 'id:' + el.id : el.name ? 'name:' + el.name : null);

  function own(el) {
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const t = lb.split(/\s+/).map((i) => T(document.getElementById(i))).join(' ').trim();
      if (t) return t;
    }
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && !GENERIC.test(T(l))) return T(l);
    }
    return el.getAttribute('aria-label') || '';
  }
  function container(el) {
    const ash = el.closest('.ashby-application-form-field-entry');
    if (ash) { const l = ash.querySelector('.ashby-application-form-question-title, label'); if (l) return T(l); }
    const lev = el.closest('.application-question');
    if (lev) { const l = lev.querySelector('.application-label .text, .application-label'); if (l) return T(l); }
    const fs = el.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg && T(lg)) return T(lg); }
    const wrap = el.closest('.field-wrapper, .select__container, .file-upload, [class*="upload"]');
    if (wrap) { const l = wrap.querySelector('label, .label, [id$="-label"]'); if (l && !GENERIC.test(T(l))) return T(l); }
    // Any other site: the nearest form-row container that holds a label-like element.
    const row = el.closest('.form-group, .form-field, .form-row, .field, .question, [class*="field"], [class*="question"], [class*="form-item"]');
    if (row) { const l = row.querySelector('label, legend, .label, [class*="label"], [class*="title"]'); if (l && !GENERIC.test(T(l))) return T(l); }
    return '';
  }
  function optionLabel(input) {
    const wrap = input.closest('label');
    if (wrap) return T(wrap);
    if (input.id) { const l = document.querySelector(`label[for="${CSS.escape(input.id)}"]`); if (l) return T(l); }
    return input.value || '';
  }

  const out = [];
  const done = new Set();
  for (const box of document.querySelectorAll('.ashby-application-form-input-yesno')) {
    const cb = box.querySelector('input[type=checkbox]');
    if (!cb || !cb.name) continue;
    const entry = box.closest('.ashby-application-form-field-entry');
    const title = entry && entry.querySelector('.ashby-application-form-question-title');
    done.add('name:' + cb.name);
    out.push({ ref: 'name:' + cb.name, label: clean(T(title)), kind: 'yesno',
               required: !!(title && /required/i.test(title.className)), options: [...box.querySelectorAll('button')].map(T) });
  }
  for (const el of document.querySelectorAll('input, select, textarea')) {
    const type = (el.type || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(type)) continue;
    if (/captcha/i.test(el.name || '') || /captcha/i.test(el.id || '')) continue;
    if (type !== 'file' && !shown(el) && !(type === 'radio' || type === 'checkbox')) continue;
    let r = ref(el);
    if (!r && type !== 'radio' && type !== 'checkbox') {
      // No id or name (e.g. Ashby location comboboxes): fall back to the question's own label text,
      // but only for a question block whose only control this is (not a widget's helper inputs).
      const entry = el.closest('.ashby-application-form-field-entry, .application-question, .form-group, .form-field, .form-row, [class*="field"], [class*="question"]');
      const others = entry ? [...entry.querySelectorAll('input, select, textarea')].filter((x) => x !== el && x.type !== 'hidden' && (x.id || x.name)) : [];
      const cl = clean(container(el));
      if (entry && !others.length && cl) r = 'label:' + cl.slice(0, 250);
    }
    if (!r || done.has(r)) continue;
    let rawLabel = own(el) || container(el);
    if (!rawLabel && type !== 'radio' && type !== 'checkbox') { const w = el.closest('label'); if (w) rawLabel = T(w); }
    const title = el.closest('.ashby-application-form-field-entry')?.querySelector('.ashby-application-form-question-title');
    const required = el.required || el.getAttribute('aria-required') === 'true' || /[*✱]/.test(rawLabel) ||
                     !!(title && /required/i.test(title.className));
    const q = { ref: r, label: clean(rawLabel), required, options: [] };
    if (type === 'radio' || type === 'checkbox') {
      const group = el.name ? [...document.querySelectorAll(`input[type=${type}][name="${CSS.escape(el.name)}"]`)] : [el];
      if (el.name) done.add('name:' + el.name);
      q.ref = el.name ? 'name:' + el.name : r;
      if (type === 'checkbox' && group.length === 1) {
        q.kind = 'checkbox';
        if (!q.label) q.label = clean(optionLabel(el));
      } else {
        q.kind = type === 'radio' ? 'radio' : 'checkbox_group';
        q.options = group.map((g) => clean(optionLabel(g)));
        q.required = q.required || group.some((g) => g.required);
        if (!q.label) q.label = container(el);
      }
    } else if (el.tagName === 'SELECT') {
      q.kind = 'select';
      q.options = [...el.options].filter((o) => o.value !== '').map((o) => o.textContent.trim());
    } else if (el.getAttribute('role') === 'combobox') {
      q.kind = 'combobox';
    } else if (type === 'file') {
      q.kind = 'file';
      q.accept = el.accept || null;
    } else if (el.tagName === 'TEXTAREA') {
      q.kind = 'textarea';
    } else {
      q.kind = { email: 'email', tel: 'tel', url: 'url', number: 'number' }[type] || 'text';
    }
    done.add(r);
    out.push(q);
  }
  return out;
}
"""


@dataclass
class Files:
    resume: str | None = None
    cover_letter: str | None = None


def pause(lo: float = 0.25, hi: float = 0.7) -> None:
    """Sleep a random short time, to act at a human pace."""
    time.sleep(random.uniform(lo, hi))  # human pace between actions


ENTRY = (
    ".ashby-application-form-field-entry, .application-question, .field-wrapper, .form-group, .form-field, "
    ".form-row, [class*='field'], [class*='question']"
)
TITLE = (
    ".ashby-application-form-question-title, .application-label, label, legend, .label, [class*='label'], "
    "[class*='title']"
)  # keep in step with container() in EXTRACT_JS


def _loc(page: Page, ref: str) -> Locator:
    """A locator for a question ref: by id, by name, or by the question block carrying a label."""
    how, _, val = ref.partition(":")
    if how == "label":  # control without id/name: find it inside the question that carries this label
        # .last: the innermost question block carrying this label (outer wrappers match too)
        entry = page.locator(ENTRY).filter(has=page.locator(TITLE, has_text=val)).last
        return entry.locator("input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea, select")
    attr = "id" if how == "id" else "name"
    return page.locator(f"[{attr}={json.dumps(val)}]")


# ---- reading ----------------------------------------------------------------------------

MAX_OPTIONS = 500


def extract(page: Page) -> list[dict]:
    """Read the form's questions from the page, opening comboboxes to list their options (capped)."""
    questions = page.evaluate(EXTRACT_JS)
    for q in questions:
        if q["kind"] == "combobox":
            q["options"] = _combobox_options(page, q["ref"])
            if not q["options"]:
                q["kind"] = "autocomplete"  # options appear only after typing (e.g. locations)
        q["options_complete"] = len(q["options"]) <= MAX_OPTIONS
        q["options"] = q["options"][:MAX_OPTIONS]
    return questions


def _combobox_options(page: Page, ref: str) -> list[str]:
    """Open a combobox, read its options (minus placeholders like 'loading'), then close it."""
    loc = _loc(page, ref).first
    try:
        loc.click(timeout=3000)
        page.wait_for_timeout(350)
        opts = _listbox(page, loc).all_inner_texts()
    except PWTimeout:
        opts = []
    finally:
        page.keyboard.press("Escape")
    opts = [
        o.strip() for o in opts if o.strip() and not re.match(r"^(no options|loading|type to search)", o.strip(), re.I)
    ]
    return opts[:500]


def _listbox(page: Page, combo: Locator) -> Locator:
    """Options of *this* combobox only: the listbox it controls, else visible options.
    (Pages keep other option lists in the DOM, e.g. a phone-country picker.)"""
    owned = combo.get_attribute("aria-controls") or combo.get_attribute("aria-owns")
    if owned:
        lb = page.locator(f"[id={json.dumps(owned)}] [role=option]")
        if lb.count():
            return lb
    return page.locator("[role=option]").filter(visible=True)


# ---- finding the form on any site ---------------------------------------------------------


class Blocked(Exception):
    """This application can't be filled automatically; the reason goes to you."""


COUNT_JS = r"""
() => [...document.querySelectorAll('input, select, textarea')].filter((e) => {
  const t = (e.type || '').toLowerCase();
  if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(t)) return false;
  if (/captcha/i.test(e.name || '') || /captcha/i.test(e.id || '')) return false;
  return t === 'file' || !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
}).length
"""
LOGIN_JS = r"""
() => {
  const pw = [...document.querySelectorAll('input[type=password]')].some((e) => e.offsetWidth || e.offsetHeight);
  const text = document.body ? document.body.innerText.slice(0, 20000) : '';
  return pw || /(sign|log) ?in to (apply|continue)|create (an |your )?account to (apply|continue)|you must (sign|log) ?in/i.test(text);
}
"""
# A site refusing automated browsers. appa never tries to get around bot protection.
BOT_WALL = re.compile(
    r"access (is )?(temporarily )?(restricted|denied)|unusual activity from your (device|network)|"
    r"automated \(?bot\)? activity|verify (that )?you are (a )?human|checking your browser|"
    r"request (was )?blocked|are you a robot",
    re.I,
)


BOT_WALL_HOSTS = (
    "captcha-delivery.com",
    "datadome.co",
    "perimeterx.net",
    "px-cloud.net",
    "px-captcha",
    "challenges.cloudflare.com",
    "kasada.io",
)


def bot_walled(page: Page) -> bool:
    """A bot-protection wall instead of the page, in the main frame or an embedded one."""
    if _count(page) >= MIN_FIELDS:
        return False
    for fr in page.frames:
        if any(h in (fr.url or "") for h in BOT_WALL_HOSTS):
            return True
        try:
            if BOT_WALL.search(fr.inner_text("body", timeout=2000)[:5000]):
                return True
        except Exception:
            continue
    return False


APPLY_TEXT = re.compile(
    r"^\s*(apply|apply now|apply for this (job|position|role)|apply online|start (your )?application|i['’]?m interested)\s*$",
    re.I,
)
NEXT_TEXT = re.compile(r"^\s*(next|continue|save (and|&) continue|next step)\b", re.I)
# Only read, never clicked: a form with Next but no Submit is a multi-page wizard (handed back to you).
SUBMIT_TEXT = re.compile(r"submit|send application|apply", re.I)
MIN_FIELDS = 2


def _count(page: Page) -> int:
    """How many form controls the page has (0 on error)."""
    try:
        return int(page.evaluate(COUNT_JS))
    except Exception:
        return 0


def _settle(page: Page) -> None:
    """Wait for network idle (up to 15s), then a little more."""
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(1200)


def find_form(page: Page, browser) -> Page:
    """Get to the application form on any site, or raise Blocked with the reason.
    Never signs in, creates accounts, or follows links off the application's site."""
    from urllib.parse import urlparse

    for _attempt in range(2):  # at most one "Apply" hop
        if bot_walled(page):
            raise Blocked("the site blocked automated browsing (bot protection)")
        if page.evaluate(LOGIN_JS):
            raise Blocked("this application needs a login or an account")
        if _count(page) >= MIN_FIELDS:
            break
        # The form may live in an embedded frame (e.g. a company page embedding an ATS)
        best, best_n = None, 0
        for fr in page.frames[1:]:
            try:
                n = int(fr.evaluate(COUNT_JS))
            except Exception:
                continue
            if n > best_n:
                best, best_n = fr, n
        if best is not None and best_n >= MIN_FIELDS:
            host = (urlparse(best.url).hostname or "").lower()
            if not browser.allows(host):
                raise Blocked(f"the form is embedded from {host}, which appa doesn't open")
            page.goto(best.url, wait_until="domcontentloaded", timeout=60000)
            _settle(page)
            continue
        # A job page with an "Apply" link or button in front of the form
        target = None
        for cand in page.locator("a, button, [role=button]").filter(visible=True).all()[:400]:
            try:
                if APPLY_TEXT.match(cand.inner_text(timeout=500) or ""):
                    target = cand
                    break
            except Exception:
                continue
        if target is None:
            # Single-page apps render late: give it a few seconds to show a form or a block page.
            for _ in range(4):
                page.wait_for_timeout(2000)
                if bot_walled(page):
                    raise Blocked("the site blocked automated browsing (bot protection)")
                if _count(page) >= MIN_FIELDS:
                    break
            else:
                raise Blocked("couldn't find an application form on this page")
            continue
        href = target.get_attribute("href")
        if href and not href.startswith(("#", "javascript:")):
            from urllib.parse import urljoin

            host = (urlparse(urljoin(page.url, href)).hostname or "").lower()
            if not browser.allows(host):
                raise Blocked(f"the Apply button leads to {host}, which appa doesn't open")
        # Click it like a person would (sites often route with JavaScript; the href can be a
        # placeholder). Same tab, so the navigation pin still checks wherever it goes.
        target.evaluate("(el) => el.removeAttribute('target')")
        before_blocked = len(browser.blocked)
        target.click(timeout=5000)
        _settle(page)
        if len(browser.blocked) > before_blocked:
            host = urlparse(browser.blocked[-1]).hostname or "?"
            raise Blocked(f"the Apply button leads to {host}, which appa doesn't open")
        page.wait_for_timeout(2500)  # single-page apps render the form after navigating
    else:
        if bot_walled(page):
            raise Blocked("the site blocked automated browsing (bot protection)")
        if _count(page) < MIN_FIELDS:
            raise Blocked("couldn't find an application form after following Apply")
    if page.evaluate(LOGIN_JS):
        raise Blocked("this application needs a login or an account")
    buttons = [
        t.strip()
        for t in page.locator("button, input[type=submit], [role=button]").filter(visible=True).all_inner_texts()[:200]
    ]
    if any(NEXT_TEXT.match(b) for b in buttons) and not any(SUBMIT_TEXT.search(b) for b in buttons):
        raise Blocked("it's a multi-page application form; apply yourself for now")
    return page


# ---- filling ----------------------------------------------------------------------------


def fill(page: Page, plan: list[dict], files: Files) -> list[dict]:
    """Fill each planned field that has a value; returns a per-field ok/error report."""
    report = []
    for item in plan:
        if item.get("value") is None:
            continue
        ok, error = True, None
        try:
            _fill_one(page, item, files)
        except Exception as e:
            ok, error = False, f"{e.__class__.__name__}: {str(e).splitlines()[0][:200]}"
        report.append({"ref": item["ref"], "label": item["label"][:200], "ok": ok, "error": error})
        pause()
    return report


def _fill_one(page: Page, item: dict, files: Files) -> None:
    """Fill one field by its kind and check it took; raises if the value or option can't be set."""
    kind, value, ref = item["kind"], item["value"], item["ref"]
    loc = _loc(page, ref)
    if kind in ("text", "email", "tel", "url", "number", "textarea"):
        el = loc.first
        el.scroll_into_view_if_needed(timeout=5000)
        el.click(timeout=5000)
        el.fill(str(value), timeout=5000)
        got = el.input_value()
        if kind == "tel":  # phone widgets reformat as you type, e.g. "(312) 555-0100"
            if not re.sub(r"\D", "", got).endswith(re.sub(r"\D", "", str(value))[-7:]):
                raise ValueError(f"phone shows {got!r}")
        elif got != str(value):
            raise ValueError("value didn't stick")
    elif kind == "select":
        sel = loc.first
        labels = [t.strip() for t in sel.locator("option").all_inner_texts()]
        target = next((t for t in labels if t.lower() == str(value).lower()), None)
        if target is None:  # long lists sent to the server were truncated; still require a clear match
            hits = [t for t in labels if str(value).lower() in t.lower()]
            target = hits[0] if len(hits) == 1 else None
        if target is None:
            raise ValueError(f"no option matching {value!r}")
        sel.select_option(label=target, timeout=5000)
    elif kind in ("combobox", "autocomplete"):
        _fill_combobox(page, loc.first, str(value), exact=kind == "combobox")
    elif kind in ("radio", "checkbox_group"):
        wanted = value if isinstance(value, list) else [value]
        boxes = page.locator(f"input[name={json.dumps(ref.partition(':')[2])}]")
        hit = 0
        for i in range(boxes.count()):
            box = boxes.nth(i)
            text = box.evaluate(
                "(el) => (el.closest('label') || {textContent: el.value}).textContent.replace(/\\s+/g,' ').trim()"
            )
            if any(text.replace("✱", "").strip().lower() == w.lower() for w in wanted):
                box.check(timeout=5000, force=True)
                hit += 1
        if hit < len(wanted):
            raise ValueError(f"option not found: {wanted}")
    elif kind == "checkbox":
        if str(value).lower() == "yes":
            loc.first.check(timeout=5000, force=True)
        else:
            loc.first.uncheck(timeout=5000, force=True)
    elif kind == "yesno":
        box = loc.first.locator("xpath=ancestor::div[contains(@class,'yesno')][1]")
        btn = box.get_by_role("button", name=str(value), exact=True)
        btn.click(timeout=5000)
        if btn.get_attribute("aria-pressed") != "true":
            raise ValueError("choice didn't register")
    elif kind == "file":
        path = {"@resume": files.resume, "@cover_letter": files.cover_letter}.get(str(value))
        if not path:
            raise ValueError(f"no file configured for {value} (runner --resume / --cover-letter)")
        loc.first.set_input_files(path, timeout=10000)
        page.wait_for_timeout(1200)  # some ATSs parse the résumé and prefill fields
    else:
        raise ValueError(f"don't know how to fill {kind}")


def _fill_combobox(page: Page, el: Locator, value: str, *, exact: bool) -> None:
    """Type into a combobox and click the matching option (or the top suggestion if not exact); raises if none."""
    el.scroll_into_view_if_needed(timeout=5000)
    el.click(timeout=5000)
    el.fill(value, timeout=5000)
    page.wait_for_timeout(700)
    options = _listbox(page, el)
    texts = [t.strip() for t in options.all_inner_texts()]
    idx = next((i for i, t in enumerate(texts) if t.lower() == value.lower()), None)
    if idx is None:
        idx = next((i for i, t in enumerate(texts) if t.lower().startswith(value.lower())), None)
    if idx is None and not exact and texts:
        idx = 0  # free-text autocomplete (locations): take the top suggestion
    if idx is None:
        page.keyboard.press("Escape")
        raise ValueError(f"no option matching {value!r}")
    options.nth(idx).click(timeout=5000)
