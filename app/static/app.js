/* ENV Manager page behaviour. Vanilla JS in an external file (strict CSP, no inline scripts).
   Handlers are delegated from the document so content swapped in by htmx works without rebinding.
   Behaviours: theme toggle, dialogs, sidebar collapse, reveal hide and timer, removing a new
   variable row, row toggle, tree toggle, copy, dismiss, history back, picking a path into a
   field, closing dialogs when the server asks, and a toast when an htmx request fails. */
(function () {
  'use strict';

  var root = document.documentElement;
  var THEME_KEY = 'env-theme';
  var NAV_KEY = 'env-nav';
  var REVEAL_MS = 30000;
  var COPIED_MS = 2000;

  function readStored(key) {
    try { return localStorage.getItem(key); } catch (error) { return null; }
  }

  function writeStored(key, value) {
    try { localStorage.setItem(key, value); } catch (error) { /* storage unavailable */ }
  }

  /* Theme is applied while the head script runs so the page never paints in the wrong theme.
     A ?theme= parameter overrides the stored choice for review. */
  var themeParam = new URLSearchParams(location.search).get('theme');
  root.classList.toggle('dark', (themeParam || readStored(THEME_KEY) || 'light') === 'dark');

  function syncThemeLabels() {
    var dark = root.classList.contains('dark');
    document.querySelectorAll('[data-js=theme-toggle]').forEach(function (button) {
      button.setAttribute('aria-label', dark ? 'Switch to light theme' : 'Switch to dark theme');
    });
  }

  function setNavCollapsed(collapsed) {
    var nav = document.querySelector('[data-nav]');
    if (!nav) { return; }
    nav.setAttribute('data-collapsed', String(collapsed));
    var toggle = nav.querySelector('[data-js=nav-toggle]');
    toggle.setAttribute('aria-expanded', String(!collapsed));
    toggle.setAttribute('aria-label', collapsed ? 'Expand navigation' : 'Collapse navigation');
  }

  function applyTree() {
    document.querySelectorAll('[data-node]').forEach(function (row) {
      var hidden = false;
      var parentId = row.getAttribute('data-parent');
      while (parentId) {
        var parent = document.querySelector('[data-node="' + parentId + '"]');
        var toggle = parent && parent.querySelector('[data-js=tree-toggle]');
        if (toggle && toggle.getAttribute('aria-expanded') === 'false') { hidden = true; break; }
        parentId = parent && parent.getAttribute('data-parent');
      }
      row.classList.toggle('hidden', hidden);
    });
  }

  var revealTimers = new WeakMap();

  /* A reveal is an audited server request whose answer carries the value. The value is only ever
     in the page while a row is revealed: hiding it, by button or after 30 seconds, removes it from
     the document, and showing it again takes another audited request. A raw text field the person
     changed is kept past the timeout so typing is never discarded. */
  function hideRevealed(row) {
    clearTimeout(revealTimers.get(row));
    revealTimers.delete(row);
    row.querySelectorAll('[data-reveal-value]').forEach(function (value) { value.remove(); });
    row.setAttribute('data-revealed', 'false');
  }

  function hasUnsavedText(row) {
    var field = row.querySelector('[data-keep-edited] textarea');
    return !!field && field.value !== field.defaultValue;
  }

  function armReveals() {
    document.querySelectorAll('[data-reveal-value]').forEach(function (value) {
      var row = value.closest('[data-reveal-row]');
      if (!row || row.getAttribute('data-revealed') === 'true') { return; }
      row.setAttribute('data-revealed', 'true');
      revealTimers.set(row, setTimeout(function () {
        if (!hasUnsavedText(row)) { hideRevealed(row); }
      }, REVEAL_MS));
    });
  }

  function copySecret(button) {
    var source = document.getElementById(button.getAttribute('data-target'));
    var label = button.querySelector('[data-copy-label]');
    if (!navigator.clipboard || !source) { return; }
    navigator.clipboard.writeText(source.value || source.textContent).then(function () {
      if (!label) { return; }
      label.textContent = 'Copied';
      setTimeout(function () { label.textContent = 'Copy'; }, COPIED_MS);
    });
  }

  function showErrorToast() {
    var template = document.getElementById('error-toast-template');
    if (!template) { return; }
    document.querySelectorAll('[data-toast-source=request]').forEach(function (old) { old.remove(); });
    var toast = template.content.firstElementChild.cloneNode(true);
    toast.setAttribute('data-toast-source', 'request');
    document.body.appendChild(toast);
  }

  var clickActions = {
    'theme-toggle': function () {
      var dark = root.classList.toggle('dark');
      writeStored(THEME_KEY, dark ? 'dark' : 'light');
      syncThemeLabels();
    },
    'dialog-open': function (control) {
      var dialog = document.getElementById(control.getAttribute('data-target'));
      if (dialog) { dialog.showModal(); }
    },
    'menu-open': function () {
      var dialog = document.getElementById('mobile-menu');
      if (dialog) { dialog.showModal(); }
    },
    'dialog-close': function (control) {
      var dialog = control.closest('dialog');
      if (dialog) { dialog.close(); }
    },
    'nav-toggle': function () {
      var nav = document.querySelector('[data-nav]');
      var collapsed = nav.getAttribute('data-collapsed') !== 'true';
      setNavCollapsed(collapsed);
      writeStored(NAV_KEY, String(collapsed));
    },
    'reveal-hide': function (button) {
      var row = button.closest('[data-reveal-row]');
      if (row) { hideRevealed(row); }
    },
    'row-remove': function (button) {
      var row = button.closest('[data-fragment=kv-row]');
      if (row) { row.remove(); }
    },
    'row-toggle': function (button) {
      var row = document.getElementById(button.getAttribute('aria-controls'));
      var open = button.getAttribute('aria-expanded') === 'true';
      button.setAttribute('aria-expanded', String(!open));
      if (row) { row.classList.toggle('hidden', open); }
    },
    'tree-toggle': function (button) {
      button.setAttribute('aria-expanded', String(button.getAttribute('aria-expanded') !== 'true'));
      applyTree();
    },
    'copy': copySecret,
    'pick-path': function (control) {
      var input = document.getElementById(control.getAttribute('data-target'));
      var path = control.getAttribute('data-path');
      var dialog = control.closest('dialog');
      if (!input || !path) { return; }
      input.value = path;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      if (dialog) { dialog.close(); }
      input.focus();
    },
    'dismiss': function (button) {
      var toast = button.closest('[data-fragment=toast]');
      if (toast) { toast.remove(); }
    },
    'history-back': function (link, event) {
      if (history.length > 1) {
        event.preventDefault();
        history.back();
      }
    }
  };

  document.addEventListener('click', function (event) {
    if (!(event.target instanceof Element)) { return; }
    if (event.target.tagName === 'DIALOG') {
      event.target.close();
      return;
    }
    var control = event.target.closest('[data-js]');
    var action = control && clickActions[control.getAttribute('data-js')];
    if (action) { action(control, event); }
  });

  /* A failed request keeps the previous content (the server answer is not swapped) and the
     person is told with a toast. Validation answers (422) are swapped as form content instead. */
  document.addEventListener('htmx:response:error', function (event) {
    var ctx = event.detail && event.detail.ctx;
    var status = ctx && ctx.response ? ctx.response.status : 0;
    if (status >= 500 || status === 403 || status === 404) { showErrorToast(); }
  });
  /* A response header event (HX-Trigger: close-dialogs) closes open dialogs after a form
     succeeded. */
  document.addEventListener('close-dialogs', function () {
    document.querySelectorAll('dialog[open]').forEach(function (dialog) {
      dialog.close();
    });
  });
  document.addEventListener('htmx:error', showErrorToast);
  document.addEventListener('htmx:after:swap', function () {
    syncThemeLabels();
    armReveals();
  });

  document.addEventListener('DOMContentLoaded', function () {
    syncThemeLabels();
    setNavCollapsed(readStored(NAV_KEY) === 'true');
  });
})();
