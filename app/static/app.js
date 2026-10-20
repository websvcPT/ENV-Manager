/* ENV Manager page behaviour. Vanilla JS in an external file (strict CSP, no inline scripts).
   Handlers are delegated from the document so content swapped in by htmx works without rebinding.
   Behaviours: theme toggle and preference (light, dark or follow the system), dialogs (one that
   held a one-time secret is emptied when it closes), the row menu below md (filled from the
   action controls of the row), sidebar collapse, reveal hide and timer, removing a new variable
   row, row toggle, tree toggle, copy, dismiss, history back, picking a path into a field,
   limiting the version comparison to two versions, closing dialogs when the server asks,
   the deploy dialog (loading content, chosen environments, closing when the load fails), the
   files of the Docker Compose import following their services, the dist file fields and the type suggestion of the file forms, and a toast when an htmx request fails (an access grid checkbox goes back to the stored state). */
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
     browser; nothing is stored on the server. Match system is the preference until one is chosen. */
  var THEME_CHOICES = ['light', 'dark', 'system'];
  var colorScheme = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

  function themePreference() {
    var stored = readStored(THEME_KEY);
    return THEME_CHOICES.indexOf(stored) >= 0 ? stored : 'system';
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

  /* One pass over the rows: a row is hidden when its parent is collapsed or hidden. The rows are
     indexed once and each answer is remembered, so the cost grows with the rows, not with the
     rows times their depth times the size of the document. */
  function applyTree() {
    var rows = document.querySelectorAll('[data-node]');
    var byId = new Map();
    var hiddenById = new Map();
    rows.forEach(function (row) { byId.set(row.getAttribute('data-node'), row); });
    function isHidden(row) {
      var id = row.getAttribute('data-node');
      if (hiddenById.has(id)) { return hiddenById.get(id); }
      var parent = byId.get(row.getAttribute('data-parent'));
      var toggle = parent && parent.querySelector('[data-js=tree-toggle]');
      var hidden = !!parent &&
        ((!!toggle && toggle.getAttribute('aria-expanded') === 'false') || isHidden(parent));
      hiddenById.set(id, hidden);
      return hidden;
    }
    rows.forEach(function (row) { row.classList.toggle('hidden', isHidden(row)); });
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

  /* The row menu (below md): one dialog in the page, filled when a row's 3-dot button opens it
     with an item for each action control of that row. The controls stay in the markup (hidden
     below md), so a row carries its actions once; an item forwards its click to its control. */
  var MENU_ITEM = 'flex h-11 w-full items-center gap-3 rounded-btn px-3 text-left text-sm font-semibold hover:bg-surface2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus';

  var SVG_NS = 'http://www.w3.org/2000/svg';

  /* An icon control carries its drawing; a text control (the primary action) names the path of
     its icon in data-icon, which is drawn here, so the row holds no hidden drawing. */
  function menuIcon(control) {
    var existing = control.querySelector('svg');
    var svg = existing ? existing.cloneNode(true) : document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', 'size-4 shrink-0');
    if (!existing) {
      var attributes = {
        viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2',
        'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true'
      };
      Object.keys(attributes).forEach(function (name) { svg.setAttribute(name, attributes[name]); });
      var shape = document.createElementNS(SVG_NS, 'path');
      shape.setAttribute('d', control.getAttribute('data-icon') || '');
      svg.appendChild(shape);
    }
    return svg;
  }

  function menuItem(control) {
    var isLink = control.tagName === 'A';
    var item = document.createElement(isLink ? 'a' : 'button');
    if (isLink) { item.setAttribute('href', control.getAttribute('href')); } else { item.type = 'button'; }
    item.className = MENU_ITEM + (control.getAttribute('data-tone') === 'danger' ? ' text-danger' : ' text-ink');
    item.setAttribute('data-js', 'row-menu-item');
    item.setAttribute('data-row-menu-item', control.getAttribute('data-row-action'));
    item.setAttribute('data-source', control.id);
    item.appendChild(menuIcon(control));
    var text = document.createElement('span');
    text.textContent = control.getAttribute('aria-label') || control.textContent.trim();
    item.appendChild(text);
    return item;
  }

  var clickActions = {
    'row-menu': function (button) {
      var dialog = document.getElementById('row-menu');
      var group = button.parentElement && button.parentElement.querySelector(':scope > [data-row-actions]');
      if (!dialog || !group) { return; }
      var subject = button.getAttribute('data-subject') || '';
      var items = Array.prototype.map.call(group.querySelectorAll('[data-row-action]'), menuItem);
      dialog.setAttribute('aria-label', 'Actions for ' + subject);
      dialog.querySelector('[data-row-menu-title]').textContent = subject;
      var list = dialog.querySelector('[data-row-menu-items]');
      list.replaceChildren.apply(list, items);
      dialog.showModal();
    },
    'row-menu-item': function (item, event) {
      var dialog = item.closest('dialog');
      var source = document.getElementById(item.getAttribute('data-source'));
      if (item.tagName !== 'A') {
        event.preventDefault();
      }
      /* Closing first puts focus back on the 3-dot button, so a confirm dialog that the action
         opens returns focus there when it closes. */
      if (dialog) { dialog.close(); }
      if (item.tagName !== 'A' && source) { source.click(); }
    },
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

  /* The Docker Compose import preview: a file follows the services that use it. An owner select
     offers only the ticked services and moves to the first of them when its own is unticked, and
     a file none of whose services is ticked is unticked and cannot be ticked until one is. */
  function syncComposeFiles(form) {
    var ticked = {};
    form.querySelectorAll('[data-js=compose-service]:checked').forEach(function (box) {
      ticked[box.value] = true;
    });
    form.querySelectorAll('[data-compose-file]').forEach(function (row) {
      var pick = row.querySelector('[data-compose-pick]');
      var owner = row.querySelector('[data-compose-owner]');
      var available = (row.getAttribute('data-users') || '').split(' ').filter(function (name) {
        return name !== '' && ticked[name];
      });
      if (owner instanceof HTMLSelectElement) {
        Array.prototype.forEach.call(owner.options, function (option) {
          option.disabled = option.value !== '' && !ticked[option.value];
        });
        if (owner.value !== '' && !ticked[owner.value]) { owner.value = available[0] || ''; }
      }
      if (!(pick instanceof HTMLInputElement)) { return; }
      if (available.length === 0) {
        pick.checked = false;
        pick.disabled = true;
      } else if (pick.disabled) {
        pick.disabled = false;
        pick.checked = pick.defaultChecked;
      }
    });
  }

  document.addEventListener('change', function (event) {
    var box = event.target;
    if (!(box instanceof HTMLInputElement) || box.getAttribute('data-js') !== 'compose-service') {
      return;
    }
    if (box.form) { syncComposeFiles(box.form); }
  });

  /* The dist file of a file form depends on its type. A certificate has no dist file: the dist
     path field is hidden and disabled (so it is not sent) and the "Copy the dist file" source of
     the add form is unavailable. For an ENV file that source is available only while the dist
     path field holds a path. Losing the source while it is chosen goes back to pasting. */
  function syncDistFields(form) {
    var type = form.querySelector('[data-js=file-type]');
    var field = form.querySelector('[data-js=dist-path]');
    var cert = type instanceof HTMLSelectElement && type.value === 'cert';
    if (field instanceof HTMLInputElement) {
      field.disabled = cert;
      form.querySelectorAll('[data-dist-field]').forEach(function (wrapper) { wrapper.hidden = cert; });
    }
    var choice = form.querySelector('[data-js=dist-source]');
    if (!(choice instanceof HTMLInputElement)) { return; }
    var empty = !(field instanceof HTMLInputElement) || field.value.trim() === '';
    var unavailable = cert || empty;
    choice.disabled = unavailable;
    var option = choice.closest('label');
    if (option) {
      option.classList.toggle('cursor-not-allowed', unavailable);
      option.classList.toggle('opacity-60', unavailable);
      option.classList.toggle('cursor-pointer', !unavailable);
      option.querySelectorAll('[data-dist-ready]').forEach(function (hint) { hint.hidden = unavailable; });
      option.querySelectorAll('[data-dist-missing]').forEach(function (hint) { hint.hidden = cert || !empty; });
      option.querySelectorAll('[data-dist-cert]').forEach(function (hint) { hint.hidden = !cert; });
    }
    var paste = form.querySelector('#src-paste');
    if (unavailable && choice.checked && paste instanceof HTMLInputElement) { paste.checked = true; }
  }

  /* The add environment form selects Certificate for a name that ends like a certificate file,
     until the person chooses the type themselves. */
  function suggestFileType(form, name) {
    var type = form.querySelector('[data-js=file-type]');
    if (!(type instanceof HTMLSelectElement) || type.hasAttribute('data-chosen')) { return; }
    var suffixes = (name.getAttribute('data-cert-suffixes') || '').split(' ').filter(Boolean);
    var typed = name.value.trim().toLowerCase();
    var cert = suffixes.some(function (suffix) { return typed.endsWith(suffix); });
    type.value = cert ? 'cert' : 'env';
    syncDistFields(form);
  }

  document.addEventListener('input', function (event) {
    var field = event.target;
    if (!(field instanceof HTMLInputElement) || !field.form) { return; }
    var kind = field.getAttribute('data-js');
    if (kind === 'dist-path') { syncDistFields(field.form); }
    if (kind === 'file-name') { suggestFileType(field.form, field); }
  });

  document.addEventListener('change', function (event) {
    var select = event.target;
    if (!(select instanceof HTMLSelectElement) || select.getAttribute('data-js') !== 'file-type') {
      return;
    }
    select.setAttribute('data-chosen', 'true');
    if (select.form) { syncDistFields(select.form); }
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
