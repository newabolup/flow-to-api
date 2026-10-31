"""In-page JavaScript used by the bridge.

Everything here runs *inside* Chromium, so a decision that used to cost
20-50 CDP round trips (`.count()`, `.is_visible()`, `inner_text()` per
element) now costs one `evaluate`, and a wait that used to poll from
Python every 2 seconds now polls in-page every 250ms with zero IPC.

The predicates intentionally read the same signals the old code read
(`document.body.innerText`, `main img` widths, aria-labels) so behaviour
is unchanged -- only the transport is cheaper.
"""

# --------------------------------------------------------------------------
# Full UI snapshot.
#
# Returns one small JSON object describing everything the flow code needs
# to make a decision. Cost: a single round trip (~10-30ms on a warm page).
# --------------------------------------------------------------------------
PROBE = r"""
() => {
  const isVis = (el) => {
    if (!el) return false;
    try {
      const r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) return false;
      const s = getComputedStyle(el);
      return s.visibility !== 'hidden' && s.display !== 'none';
    } catch (e) { return false; }
  };
  const txt = (el) => (el ? (el.innerText || el.textContent || '') : '');
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const body = document.body ? (document.body.innerText || '') : '';
  const low = body.toLowerCase();

  // --- composer -----------------------------------------------------------
  const composerEl = document.querySelector('[aria-label="What do you want to create?"]');
  const composer = {
    present: !!composerEl,
    visible: isVis(composerEl),
    text: clean(txt(composerEl)),
  };

  // --- timeline cards (wide images are result cards) ----------------------
  const cards = [];
  document.querySelectorAll('main img').forEach((img) => {
    const r = img.getBoundingClientRect();
    if (r.width > 150) {
      const src = img.currentSrc || img.src || '';
      if (src) cards.push({src: src, w: Math.round(r.width), y: Math.round(r.top)});
    }
  });

  // --- open menus / dropdown rows ----------------------------------------
  const menu = [];
  document.querySelectorAll('[role="option"],[role="menuitem"],.item-text .label,mat-option')
    .forEach((el) => {
      if (!isVis(el)) return;
      const t = clean(txt(el));
      if (t && t.length <= 80 && menu.indexOf(t) === -1) menu.push(t);
    });

  // --- visible buttons (name + aria-label) -------------------------------
  const buttons = [];
  document.querySelectorAll('button,[role="button"],[role="menuitem"],[role="switch"],a[role="button"]')
    .forEach((el) => {
      if (!isVis(el) || buttons.length >= 60) return;
      const t = clean(txt(el)).slice(0, 60);
      const al = el.getAttribute('aria-label') || '';
      if (t || al) buttons.push({t: t, al: al});
    });
  const hasButton = (needle) => buttons.some(
    (b) => b.t.indexOf(needle) !== -1 || b.al.indexOf(needle) !== -1);

  // --- agent toggle ------------------------------------------------------
  let agent = null;
  const agentEl = document.querySelector('[aria-label="Agent"]');
  if (isVis(agentEl)) {
    for (const a of ['aria-checked', 'aria-pressed', 'aria-selected']) {
      const v = agentEl.getAttribute(a);
      if (v === 'true' || v === 'false') { agent = (v === 'true'); break; }
    }
    if (agent === null) {
      const cls = agentEl.className || '';
      if (cls.indexOf('unchecked') !== -1) agent = false;
      else if (cls.indexOf('checked') !== -1) agent = true;
    }
  }
  if (agent === null) {
    // Fallback used by the old code: the Agent panel chrome only exists
    // when the Agent is enabled.
    agent = low.indexOf('agent instructions') !== -1;
  }

  // --- flow-toggles groups (Mode / resolution / duration / count) --------
  const toggles = {};
  document.querySelectorAll('flow-toggles[aria-label]').forEach((g) => {
    if (!isVis(g)) return;
    const label = g.getAttribute('aria-label');
    const rows = [];
    g.querySelectorAll('mat-button-toggle').forEach((t) => {
      rows.push({text: clean(txt(t)), checked: t.getAttribute('aria-checked') === 'true'});
    });
    toggles[label] = rows;
  });

  // --- misc signals ------------------------------------------------------
  const credit = body.match(/Generating will use (\d+) credits/);
  const progressVisible = [...document.querySelectorAll(
      'mat-progress-bar,[role="progressbar"]')].some(isVis)
    || /uploading|\b\d{1,3}%/.test(low);
  const summary = [];
  document.querySelectorAll('button,[role="button"]').forEach((b) => {
    if (!isVis(b)) return;
    const t = clean(txt(b));
    if (t && (t.indexOf('\u00b7') !== -1 || t.indexOf('Nano Banana') !== -1)) {
      summary.push(t.slice(0, 120));
    }
  });

  return {
    url: location.href,
    body: body,
    low: low,
    composer: composer,
    cards: cards,
    menu: menu,
    buttons: buttons,
    agent: agent,
    toggles: toggles,
    download: hasButton('Download media'),
    settings_trigger: !!document.querySelector('[aria-label="Settings trigger"]')
        && isVis(document.querySelector('[aria-label="Settings trigger"]')),
    popup_open: isVis(document.querySelector('flow-toggles[aria-label="Mode"]')),
    panel_open: low.indexOf('agent settings') !== -1 || low.indexOf('agent instructions') !== -1,
    sign_in_wall: low.indexOf('sign in') !== -1 && low.indexOf('new project') === -1,
    new_project: low.indexOf('new project') !== -1,
    thinking: low.indexOf('thinking') !== -1,
    failed: low.indexOf('something went wrong') !== -1 || low.indexOf('failed to generate') !== -1,
    credit_total: credit ? parseInt(credit[1], 10) : null,
    progress: progressVisible,
    summary: summary,
    videos: document.querySelectorAll('main video').length,
    file_inputs: document.querySelectorAll('input[type=file]').length,
    prompt_chips: document.querySelectorAll(
      '[aria-label="What do you want to create?"] img,'
      + '[aria-label="What do you want to create?"] video,'
      + '.prompt-chip, .chip').length,
    dialogs: [...document.querySelectorAll('mat-dialog-container,[role="dialog"]')].filter(isVis).length,
  };
}
"""

# --------------------------------------------------------------------------
# Wait predicates: these run inside the page under wait_for_function, so
# they must be self-contained and return a truthy value when the wait is
# satisfied (Playwright resolves with that value).
# --------------------------------------------------------------------------

# Generation finished. `baseline` is the list of result-card srcs that
# already existed before the prompt was submitted, so a reused thread with
# old cards / old completion phrases cannot be mistaken for this job.
#
# Returns "done" | "failed" | false. Same completion signals as the old
# Python loop: prompt box idle again + a completion phrase (or no Thinking
# indicator) + at least one card that is NOT in the baseline.
RESULT = r"""
(baseline) => {
  const base = baseline || [];
  const body = document.body ? (document.body.innerText || '') : '';
  const low = body.toLowerCase();
  if (low.indexOf('something went wrong') !== -1
      || low.indexOf('failed to generate') !== -1) return 'failed';
  const cards = [];
  document.querySelectorAll('main img').forEach((img) => {
    const r = img.getBoundingClientRect();
    if (r.width > 150) {
      const src = img.currentSrc || img.src || '';
      if (src && base.indexOf(src) === -1) cards.push(src);
    }
  });
  if (!cards.length) return false;
  const idle = low.indexOf('what do you want to create?') !== -1;
  if (!idle) return false;
  const done = low.indexOf('thinking') === -1
    || low.indexOf("i've generated") !== -1
    || low.indexOf("i've updated") !== -1
    || low.indexOf('here are the two images') !== -1
    || low.indexOf('here are the images') !== -1
    || low.indexOf('here is the image') !== -1;
  return done ? 'done' : false;
}
"""

# Generation finished, no freshness snapshot (video path: the result is a
# <video> element, so "some media exists" is the signal).
RESULT_ANY = r"""
() => {
  const body = document.body ? (document.body.innerText || '') : '';
  const low = body.toLowerCase();
  if (low.indexOf('something went wrong') !== -1
      || low.indexOf('failed to generate') !== -1) return 'failed';
  const isVis = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 150 && r.height > 40;
  };
  const media = [...document.querySelectorAll('main img')].some(isVis)
    || document.querySelectorAll('main video').length > 0;
  if (!media) return false;
  const idle = low.indexOf('what do you want to create?') !== -1;
  const done = low.indexOf('thinking') === -1
    || low.indexOf("i've generated") !== -1
    || low.indexOf("i've updated") !== -1;
  return (idle && done) ? 'done' : false;
}
"""

# Composer usable (no side panel / viewer stealing it).
COMPOSER = r"""
() => {
  const el = document.querySelector('[aria-label="What do you want to create?"]');
  if (!el) return false;
  const r = el.getBoundingClientRect();
  if (r.width < 1 || r.height < 1) return false;
  const s = getComputedStyle(el);
  return s.visibility !== 'hidden' && s.display !== 'none';
}
"""

# A named control is visible (used instead of sleep-then-hope).
HAS_BUTTON = r"""
(name) => {
  const isVis = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  const t = (el) => ((el.innerText || el.textContent || '') + ' '
    + (el.getAttribute('aria-label') || '')).replace(/\s+/g, ' ');
  return [...document.querySelectorAll('button,[role="button"],[role="menuitem"]')]
    .some((el) => isVis(el) && t(el).indexOf(name) !== -1);
}
"""

# Generic text probe (visible body text contains `needle`, case-insensitive).
HAS_TEXT = r"""
(needle) => (document.body ? (document.body.innerText || '') : '')
  .toLowerCase().indexOf(String(needle).toLowerCase()) !== -1
"""

# Upload finished: no progress bar and no "Uploading" banner left.
UPLOAD_IDLE = r"""
() => {
  const isVis = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  const busy = [...document.querySelectorAll('mat-progress-bar,[role="progressbar"]')].some(isVis);
  const body = (document.body ? (document.body.innerText || '') : '').toLowerCase();
  const uploading = body.indexOf('uploading') !== -1 || /\b\d{1,3}%/.test(body);
  return !busy && !uploading;
}
"""

WAIT_PREDICATES = {
    "result": RESULT,
    "result_any": RESULT_ANY,
    "composer": COMPOSER,
    "button": HAS_BUTTON,
    "text": HAS_TEXT,
    "upload_idle": UPLOAD_IDLE,
}

# --------------------------------------------------------------------------
# Helpers that must mutate the DOM to make a plain Playwright click land on
# the *right* card (the newest result, not the previous job's).
# --------------------------------------------------------------------------
MARK_CARD_BY_SRC = r"""
(src) => {
  document.querySelectorAll('[data-flowbridge-target]')
    .forEach((el) => el.removeAttribute('data-flowbridge-target'));
  const hit = [...document.querySelectorAll('main img')].find((img) => {
    const s = img.currentSrc || img.src || '';
    return s === src && img.getBoundingClientRect().width > 150;
  });
  if (!hit) return false;
  hit.setAttribute('data-flowbridge-target', '1');
  hit.scrollIntoView({block: 'center'});
  return true;
}
"""

# Clear prompt composer before typing a new prompt (contenteditable safe).
CLEAR_COMPOSER = r"""
() => {
  const el = document.querySelector('[aria-label="What do you want to create?"]');
  if (!el) return false;
  el.focus();
  const sel = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(el);
  sel.removeAllRanges();
  sel.addRange(range);
  return true;
}
"""
