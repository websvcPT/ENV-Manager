/* ENV Manager page behaviour. Vanilla JS in an external file (strict CSP, no inline scripts).
   Handlers are delegated from the document so content swapped in by htmx works without rebinding.
   Behaviours: theme toggle and preference (light, dark or follow the system), dialogs (one that
   held a one-time secret is emptied when it closes), sidebar collapse, reveal hide and timer,
   removing a new variable row, row toggle, tree toggle, copy, dismiss, history back, picking a path into a
   field, limiting the version comparison to two versions, closing dialogs when the server asks,
   the deploy dialog (loading content, chosen environments, closing when the load fails), and a
   toast when an htmx request fails (an access grid checkbox goes back to the stored state). */
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

  /* The theme preference is light, dark or system (follow the operating system), kept in the
     browser; nothing is stored on the server. Light is the preference until one is chosen. */
  var THEME_CHOICES = ['light', 'dark', 'system'];
  var colorScheme = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

  function themePreference() {
    var stored = readStored(THEME_KEY);
    return THEME_CHOICES.indexOf(stored) >= 0 ? stored : 'light';
  }

  /* Theme is applied while the head script runs so the page never paints in the wrong theme.
     A ?theme= parameter overrides the stored choice for review. */
  function applyTheme() {
    var param = new URLSearchParams(location.search).get('theme');
    var choice = THEME_CHOICES.indexOf(param) >= 0 ? param : themePreference();
    var dark = choice === 'dark' || (choice === 'system' && !!colorScheme && colorScheme.matches);
    root.classList.toggle('dark', dark);
  }

  applyTheme();

  function syncThemeLabels() {
    var dark = root.classList.contains('dark');
    document.querySelectorAll('[data-js=theme-toggle]').forEach(function (button) {
      button.setAttribute('aria-label', dark ? 'Switch to light theme' : 'Switch to dark theme');
    });
  }

  /* The account page's options show the stored preference, whichever way it was changed. */
  function syncThemeChoices() {
    var preference = themePreference();
    document.querySelectorAll('[data-js=theme-choice]').forEach(function (option) {
      option.checked = option.value === preference;
    });
  }

  document.addEventListener('change', function (event) {
    var option = event.target;
    if (!(option instanceof HTMLInputElement) || option.getAttribute('data-js') !== 'theme-choice') {
      return;
    }
    writeStored(THEME_KEY, option.value);
    applyTheme();
    syncThemeLabels();
  });

  if (colorScheme) {
    colorScheme.addEventListener('change', function () {
      applyTheme();
      syncThemeLabels();
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

  function showCopied(label) {
    if (!label) { return; }
    label.textContent = 'Copied';
    setTimeout(function () { label.textContent = 'Copy'; }, COPIED_MS);
  }

  /* The clipboard API needs a secure context. Where the app is served over plain http (a
     development stack, an internal host) the text is selected and copied with the older
     command, and stays selected so it can be copied by hand if that is refused too. */
  function copySecret(button) {
    var source = document.getElementById(button.getAttribute('data-target'));
    var label = button.querySelector('[data-copy-label]');
    if (!source) { return; }
    var text = source.value || source.textContent;
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { showCopied(label); });
      return;
    }
    if (source.select) { source.select(); }
    var copied = false;
    try { copied = document.execCommand('copy'); } catch (error) { copied = false; }
    if (copied) { showCopied(label); }
  }

  function showErrorToast() {
    var template = document.getElementById('error-toast-template');
    if (!template) { return; }
    document.querySelectorAll('[data-toast-source=request]').forEach(function (old) { old.remove(); });
    var toast = template.content.firstElementChild.cloneNode(true);
    toast.setAttribute('data-toast-source', 'request');
    document.body.appendChild(toast);
  }

  /* A dialog whose content is loaded by a request (the deploy dialog) names a template with its
     loading content: opening it again never shows the previous answer while the new one is on
     its way. */
  function showLoading(dialog) {
    var template = document.getElementById(dialog.getAttribute('data-loading') || '');
    if (!template) { return; }
    dialog.replaceChildren(template.content.cloneNode(true));
  }

  /* A request that failed while its dialog still shows the loading content has nothing to
     show: the dialog closes and the error toast explains. */
  function closeLoadingDialogs() {
    document.querySelectorAll('dialog[open]').forEach(function (dialog) {
      if (dialog.querySelector('[aria-busy=true]')) { dialog.close(); }
    });
  }

  /* A dialog that can hold a one-time secret (the generated password of a new user) is emptied
     the moment it closes, so the secret leaves the page and a later opening starts clean. The
     close event does not bubble, so it is caught on the way down. */
  document.addEventListener('close', function (event) {
    var dialog = event.target;
    if (dialog instanceof HTMLDialogElement && dialog.hasAttribute('data-clear-on-close')) {
      showLoading(dialog);
    }
  }, true);

  var clickActions = {
    'theme-toggle': function () {
      var dark = root.classList.toggle('dark');
      writeStored(THEME_KEY, dark ? 'dark' : 'light');
      syncThemeLabels();
      syncThemeChoices();
    },
    'dialog-open': function (control) {
      var dialog = document.getElementById(control.getAttribute('data-target'));
      if (!dialog) { return; }
      showLoading(dialog);
      dialog.showModal();
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
      var id = button.getAttribute('aria-controls');
      var row = document.getElementById(id);
      var open = button.getAttribute('aria-expanded') === 'true';
      /* A row can carry more than one toggle (one per breakpoint); they all follow the row. */
      document.querySelectorAll('[data-js=row-toggle]').forEach(function (other) {
        if (other.getAttribute('aria-controls') === id) {
          other.setAttribute('aria-expanded', String(!open));
        }
      });
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

  /* The history table compares two versions at most: choosing a third unchecks the earliest
     choice. This runs in the capture phase so the form's own change handler (htmx) reads the
     corrected choice. */
  var pickCounter = 0;
  document.addEventListener('change', function (event) {
    var box = event.target;
    if (!(box instanceof HTMLInputElement) || box.getAttribute('data-js') !== 'compare-version') {
      return;
    }
    if (!box.checked) { return; }
    box.setAttribute('data-picked', String(++pickCounter));
    var checked = Array.prototype.slice.call(
      document.querySelectorAll('[data-js=compare-version]:checked')
    );
    if (checked.length <= 2) { return; }
    var earliest = checked.filter(function (other) { return other !== box; })
      .sort(function (a, b) {
        return Number(a.getAttribute('data-picked') || 0) - Number(b.getAttribute('data-picked') || 0);
      })[0];
    earliest.checked = false;
  }, true);

  /* The deploy dialog: the Deploy button counts the chosen environments, and any change of the
     choice (or of the force option) marks the dry run on screen as out of date. */
  document.addEventListener('change', function (event) {
    var box = event.target;
    if (!(box instanceof HTMLInputElement)) { return; }
    var kind = box.getAttribute('data-js');
    if (kind !== 'deploy-select' && kind !== 'deploy-stale') { return; }
    var form = box.closest('form');
    if (!form) { return; }
    form.setAttribute('data-stale', 'true');
    var button = form.querySelector('[data-deploy-submit]');
    if (!button) { return; }
    var count = form.querySelectorAll('[data-js=deploy-select]:checked').length;
    button.disabled = count === 0;
    button.textContent = count === 0 ? button.getAttribute('data-label-none')
      : count === 1 ? button.getAttribute('data-label-one')
        : button.getAttribute('data-label-many').replace('{count}', String(count));
  });

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

  /* An access grid checkbox flips before the server answers. When the request fails the box goes
     back to what is stored, so the grid never shows a grant that does not exist. A refusal that
     redraws the cell replaces the box anyway. */
  function restoreGridToggle(event) {
    var ctx = event.detail && event.detail.ctx;
    var source = ctx && ctx.sourceElement;
    if (source instanceof HTMLInputElement && source.hasAttribute('data-grid-toggle')) {
      source.checked = source.defaultChecked;
    }
  }

  /* A failed request keeps the previous content (the server answer is not swapped) and the
     person is told with a toast. Validation answers (422) are swapped as form content instead. */
  document.addEventListener('htmx:response:error', function (event) {
    var ctx = event.detail && event.detail.ctx;
    var status = ctx && ctx.response ? ctx.response.status : 0;
    restoreGridToggle(event);
    if (status >= 500 || status === 403 || status === 404) {
      closeLoadingDialogs();
      showErrorToast();
    }
  });
  /* A response header event (HX-Trigger: close-dialogs) closes open dialogs after a form
     succeeded. */
  document.addEventListener('close-dialogs', function () {
    document.querySelectorAll('dialog[open]').forEach(function (dialog) {
      dialog.close();
    });
  });
  document.addEventListener('htmx:error', function (event) {
    restoreGridToggle(event);
    closeLoadingDialogs();
    showErrorToast();
  });
  document.addEventListener('htmx:after:swap', function () {
    syncThemeLabels();
    syncThemeChoices();
    armReveals();
  });

  document.addEventListener('DOMContentLoaded', function () {
    syncThemeLabels();
    syncThemeChoices();
    setNavCollapsed(readStored(NAV_KEY) === 'true');
  });
})();
