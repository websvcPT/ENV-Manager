"""Rendering tests for the component macros."""

import re
from pathlib import Path
from typing import Any

import pytest
from jinja2 import UndefinedError

from app.core.templating import environment
from app.models import UserGroup


def render_snippet(source: str, **context: object) -> str:
    """Render a template snippet through the application's Jinja environment."""
    return environment.from_string(source).render(**context)


def test_badge_maps_status_to_tone_icon_and_label() -> None:
    """A status key picks its tone classes, icon and default label; a label can override."""
    drift = render_snippet("{% from 'macros/badges.html' import badge %}{{ badge('drift') }}")
    assert "bg-warn-bg text-warn-fg" in drift
    assert 'data-status="drift"' in drift
    assert "<svg" in drift and drift.endswith("Drift</span>")
    counted = render_snippet(
        "{% from 'macros/badges.html' import badge %}{{ badge('blocked', '2 environments') }}"
    )
    assert "bg-err-bg text-err-fg" in counted and counted.endswith("2 environments</span>")


def test_role_badge_accepts_a_user_group() -> None:
    """The role badge renders from a user group value."""
    page = render_snippet(
        "{% from 'macros/badges.html' import role_badge %}{{ role_badge(group) }}",
        group=UserGroup.SUPERADMIN,
    )
    assert "bg-ink text-surface" in page and "Superadmin" in page


def test_dist_badge_maps_each_outcome_to_the_handoff_badges() -> None:
    """Blocked, warning, passed, skipped and failed checks use the shared tones and labels."""
    expected = {
        "blocked": ("bg-err-bg text-err-fg", "Blocked"),
        "warning": ("bg-warn-bg text-warn-fg", "Warning"),
        "passed": ("bg-ok-bg text-ok-fg", "Passed"),
        "skipped": ("bg-mute-bg text-mute-fg", "Skipped"),
        "failed": ("bg-err-bg text-err-fg", "Check failed"),
    }
    for outcome, (tone, label) in expected.items():
        badge = render_snippet(
            "{% from 'macros/badges.html' import dist_badge %}{{ dist_badge(outcome) }}",
            outcome=outcome,
        )
        assert tone in badge and badge.endswith(f"{label}</span>"), outcome
    with pytest.raises(UndefinedError):
        render_snippet("{% from 'macros/badges.html' import dist_badge %}{{ dist_badge('nope') }}")


def test_unknown_badge_and_icon_names_fail_loudly() -> None:
    """A typo is an error, not a silently empty control."""
    with pytest.raises(UndefinedError):
        render_snippet("{% from 'macros/badges.html' import badge %}{{ badge('nope') }}")
    with pytest.raises(UndefinedError):
        render_snippet("{% from 'macros/icons.html' import icon %}{{ icon('nope') }}")


def test_icon_sets_size_and_stroke() -> None:
    """The icon macro takes its size classes and stroke weight from the caller."""
    svg = render_snippet(
        "{% from 'macros/icons.html' import icon %}{{ icon('check', 'size-3', '2.6') }}"
    )
    assert 'class="size-3 shrink-0"' in svg and 'stroke-width="2.6"' in svg
    assert 'aria-hidden="true"' in svg


def test_button_kinds_links_and_attributes() -> None:
    """Kinds change the classes, an href makes a link, underscores become dashes."""
    source = (
        "{% from 'macros/buttons.html' import button %}"
        "{{ button('Go', kind='primary', hx_post='/projects', data_js='dialog-open') }}"
        "{{ button('Open', href='/x') }}"
        "{{ button('Off', kind='danger', disabled=true) }}"
    )
    page = render_snippet(source)
    assert "bg-accent" in page and 'hx-post="/projects"' in page and 'data-js="dialog-open"' in page
    assert '<a href="/x"' in page
    assert re.search(r'<button type="button" disabled class="[^"]*border-danger', page)
    assert "disabled:opacity-60" in page


def test_icon_button_uses_the_label_as_accessible_name() -> None:
    """Icon-only buttons carry an aria-label."""
    page = render_snippet(
        "{% from 'macros/buttons.html' import icon_button %}"
        "{{ icon_button('Close', 'close', shape='square', data_js='dialog-close') }}"
    )
    assert 'aria-label="Close"' in page and "size-9" in page


def test_field_escapes_values_and_wires_the_error() -> None:
    """Values are escaped; an error is announced and referenced by the control."""
    page = render_snippet(
        "{% from 'macros/forms.html' import field %}"
        "{{ field('name', 'Name', value=value, error='Taken', mono=true) }}",
        value='"><script>alert(1)</script>',
    )
    assert "<script>" not in page
    assert 'aria-invalid="true"' in page and 'aria-describedby="name-e"' in page
    assert 'role="alert"' in page and "border-danger" in page and "font-mono" in page
    assert '<label for="name"' in page


def test_field_hint_replaces_error_wiring() -> None:
    """Without an error the hint is referenced instead."""
    page = render_snippet(
        "{% from 'macros/forms.html' import field %}{{ field('a', 'A', hint='Short.') }}"
    )
    assert 'aria-describedby="a-h"' in page and "aria-invalid" not in page


def test_form_controls_render_state() -> None:
    """Select, checkbox, radio, dangerous option and the CSRF field keep their state."""
    page = render_snippet(
        "{% from 'macros/forms.html' import select_field, checkbox, radio_option,"
        " dangerous_option, csrf_field %}"
        "{{ select_field('role', 'Role', [('viewer', 'Viewer'), ('admin', 'Admin')], 'admin') }}"
        "{{ checkbox('go', 'Go', checked=true) }}"
        "{{ radio_option('p', 'halt', 'Halt', 'Stop.', checked=true) }}"
        "{{ dangerous_option('force', 'Force overwrite', 'Overrides policy.') }}"
        "{{ csrf_field('tok') }}"
    )
    assert '<option value="admin" selected>' in page
    assert 'name="go" checked' in page
    assert "bg-accent-soft" in page
    assert "border-danger bg-danger-soft" in page
    assert '<input type="hidden" name="csrf_token" value="tok">' in page


def test_toast_roles_follow_the_tone() -> None:
    """Errors are announced as alerts, other tones as status; floating pins the toast."""
    source = (
        "{% from 'macros/feedback.html' import toast %}"
        "{{ toast('error', 'Failed.', floating=true) }}{{ toast('success', 'Saved.') }}"
    )
    page = render_snippet(source)
    assert 'role="alert"' in page and 'role="status"' in page
    assert "fixed bottom-4 right-4" in page
    assert page.count('data-js="dismiss"') == 2


def test_feedback_components() -> None:
    """Banner, note, empty state, inline error and skeleton render their roles."""
    page = render_snippet(
        "{% from 'macros/feedback.html' import banner, note, empty_state, inline_error, skeleton %}"
        "{{ banner('Backup failed.', 'Open settings', '/settings') }}"
        "{{ note('Scoped.') }}{{ empty_state('Nothing', 'Ask.') }}"
        "{{ inline_error('Could not update', 'Try again.', hx_get='/x', hx_target='#y') }}"
        "{{ skeleton(2) }}"
    )
    assert 'href="/settings"' in page and 'role="note"' in page
    assert 'data-fragment="empty-state"' in page
    assert 'data-fragment="inline-error"' in page and 'hx-get="/x"' in page
    assert 'aria-busy="true"' in page and page.count("motion-safe:animate-pulse") == 2


def test_dialog_and_confirm_dialog() -> None:
    """A confirm dialog names its effect, offers Cancel first and passes request attributes."""
    page = render_snippet(
        "{% from 'macros/dialogs.html' import confirm_dialog %}"
        "{{ confirm_dialog('del', 'Delete it', 'Cannot be undone.', 'Delete', hx_delete='/x') }}"
    )
    assert '<dialog id="del" aria-labelledby="del-t"' in page
    assert "w-[560px]" in page and 'data-js="dialog-close"' in page
    assert page.index("Cancel") < page.index(">Delete</button>")
    assert 'hx-delete="/x"' in page and "border-danger" in page


def test_panel_table_tabs_and_breadcrumb() -> None:
    """Panel, table, tabs and breadcrumb produce the expected landmarks."""
    page = render_snippet(
        "{% from 'macros/panels.html' import panel, table, row_header, cell, tabs, breadcrumb,"
        " page_header %}"
        "{{ page_header('Projects', 'All.') }}"
        "{% call panel('Environments', 'env-t', fragment='env-panel', hx_get='/e') %}"
        "{% call table('Environments', ['Name', 'Actions'], [1]) %}"
        "<tr>{{ row_header('.env') }}{% call cell('right') %}x{% endcall %}</tr>"
        "{% endcall %}{% endcall %}"
        "{{ tabs([{'label': 'A', 'href': '/a', 'count': 2}, {'label': 'B', 'href': '/b',"
        " 'superadmin': true}], '/a') }}"
        "{{ breadcrumb([('Projects', '/projects'), ('x', none)]) }}"
    )
    assert 'aria-labelledby="env-t"' in page and 'data-fragment="env-panel"' in page
    assert 'hx-get="/e"' in page
    assert '<th scope="col"' in page and '<th scope="row"' in page
    assert "text-right" in page
    assert page.count('aria-current="page"') == 2
    assert "Superadmin</span>" in page
    assert '<li aria-hidden="true">/</li>' in page


def test_loaded_dialog_has_loading_content_a_template_and_the_clear_on_close_marker() -> None:
    """A loaded dialog starts as a busy skeleton and keeps a template to put back on open."""
    page = render_snippet(
        "{% from 'macros/dialogs.html' import loaded_dialog %}"
        "{{ loaded_dialog('user-create', 'Create user', width='720', clear_on_close=true,"
        " narrows=true) }}"
    )
    assert 'id="user-create"' in page and 'aria-labelledby="user-create-t"' in page
    assert 'data-loading="user-create-loading"' in page and "data-clear-on-close" in page
    assert "w-[720px]" in page and "has-[[data-narrow]]:w-[560px]" in page
    assert page.count('aria-busy="true"') == 4 and '<template id="user-create-loading">' in page
    plain = render_snippet(
        "{% from 'macros/dialogs.html' import loaded_dialog %}{{ loaded_dialog('d', 'Title') }}"
    )
    assert "data-clear-on-close" not in plain and "has-[" not in plain and "w-[640px]" in plain


def test_loaded_dialog_can_widen_for_a_result_wider_than_its_form() -> None:
    """The created state of a token is drawn wider than the form that leads to it."""
    wide = render_snippet(
        "{% from 'macros/dialogs.html' import loaded_dialog %}"
        "{{ loaded_dialog('token-create', 'Create API token', width='640', widens=true) }}"
    )
    assert "w-[640px]" in wide and "has-[[data-wide]]:w-[720px]" in wide
    assert "has-[[data-narrow]]" not in wide


def test_inline_error_in_a_dialog_is_a_rounded_block_and_can_drop_retry() -> None:
    """The band under a panel title becomes a block inside a dialog body."""
    band = render_snippet(
        "{% from 'macros/feedback.html' import inline_error %}"
        "{{ inline_error('Failed', 'Try later', hx_get='/x') }}"
    )
    assert "border-t border-line" in band and "Retry" in band and 'hx-get="/x"' in band
    block = render_snippet(
        "{% from 'macros/feedback.html' import inline_error %}"
        "{{ inline_error('Failed', 'Try later', in_dialog=true, retry=false) }}"
    )
    assert "rounded-panel" in block and "border-t" not in block and "Retry" not in block


def test_grant_tree_offers_projects_deployments_and_environments_with_services_as_context() -> None:
    """Only the three grant levels are checkboxes; services are text, hints name future items."""
    tree = [
        {
            "id": 1,
            "name": "Fancy Proj. Name",
            "deployments": [
                {
                    "id": 2,
                    "name": "prod",
                    "services": [
                        {
                            "id": 3,
                            "name": "mysql",
                            "environments": [{"id": 4, "name": ".env.db"}],
                        }
                    ],
                }
            ],
        }
    ]
    page = render_snippet(
        "{% from 'macros/grants.html' import grant_tree %}{{ grant_tree(tree, granted) }}",
        tree=tree,
        granted={"deployment:2"},
    )
    assert page.count('type="checkbox"') == 3
    for key in ("project:1", "deployment:2", "environment:4"):
        assert f'value="{key}"' in page and 'name="grant"' in page
    assert 'value="deployment:2" checked' in page
    assert 'value="project:1" checked' not in page and 'value="environment:4" checked' not in page
    assert "Includes future deployments, services and environments." in page
    assert "Includes future services and environments." in page
    assert "(groups environments, not a grant level)" in page and "service:" not in page
    empty = render_snippet(
        "{% from 'macros/grants.html' import grant_tree %}{{ grant_tree([], []) }}"
    )
    assert "There are no projects yet." in empty and "checkbox" not in empty
    escaped = render_snippet(
        "{% from 'macros/grants.html' import grant_tree %}{{ grant_tree(tree, []) }}",
        tree=[{"id": 1, "name": "<b>x</b>", "deployments": []}],
    )
    assert "<b>x</b>" not in escaped and "&lt;b&gt;x&lt;/b&gt;" in escaped


def test_logo_mark_is_a_decorative_image_that_follows_the_surface() -> None:
    """The solid tile is the default; the navy sidebar and menu ask for the transparent drawing."""
    solid = render_snippet(
        "{% from 'macros/shell.html' import logo_mark %}{{ logo_mark('size-7') }}"
    )
    assert solid.startswith('<img src="/static/brand/env-mark-solid.svg" alt="" ')
    assert 'class="size-7 shrink-0"' in solid and 'width="64" height="64"' in solid
    on_nav = render_snippet(
        "{% from 'macros/shell.html' import logo_mark %}{{ logo_mark('size-9', true) }}"
    )
    assert on_nav.startswith('<img src="/static/brand/env-mark-transparent.svg" alt="" ')
    assert 'class="size-9 shrink-0"' in on_nav
    assert "aria-hidden" not in solid + on_nav and ">E<" not in solid + on_nav


def test_brand_lockup_is_the_vertical_solid_drawing_with_the_caller_size() -> None:
    """The lockup is decorative: the product name stays as heading text beside it."""
    lockup = render_snippet(
        "{% from 'macros/shell.html' import brand_lockup %}{{ brand_lockup('h-24 w-auto') }}"
    )
    assert lockup.startswith('<img src="/static/brand/env-vertical-solid.svg" alt="" ')
    assert 'class="h-24 w-auto shrink-0"' in lockup and 'width="108" height="116"' in lockup


ROW_ACTIONS = (
    "{% from 'macros/row_actions.html' import row_actions %}"
    "{{ row_actions('env-1', '.env.db', actions) }}"
)
LINKS: list[dict[str, Any]] = [
    {"key": "edit", "label": "Edit", "icon": "pencil", "href": "/environments/1", "primary": True},
    {"key": "history", "label": "History", "icon": "clock", "href": "/environments/1/history"},
    {
        "key": "remove",
        "label": "Remove",
        "icon": "trash",
        "danger": True,
        "attrs": {"data_js": "dialog-open", "data_target": "confirm", "hx_get": "/x?a=1&b=2"},
    },
]


def test_row_actions_draw_icon_buttons_with_a_tooltip_from_md_and_a_menu_button_below() -> None:
    """Several actions: tooltips are the accessible names, a primary text link, a 3-dot button."""
    page = render_snippet(ROW_ACTIONS, actions=LINKS)
    desktop, phone = page.split('<button type="button" id="env-1-more"')
    assert page.count("data-row-actions") == 1 and "<dialog" not in page
    controls = {
        match.group(1): match.group(0)
        for match in re.finditer(r'<(?:a|button) [^>]*id="env-1-([a-z]+)"[^>]*>', desktop)
    }
    assert list(controls) == ["edit", "history", "remove"]
    edit = controls["edit"]
    assert "aria-label" not in edit and "after:" not in edit
    edit_link = re.search(r'<a [^>]*id="env-1-edit".*?</a>', desktop, re.DOTALL)
    assert edit_link is not None and "<svg" not in edit_link.group(0)
    assert 'data-icon="M12 20h9' in edit and ">Edit</a>" in desktop
    for label in ("history", "remove"):
        classes = controls[label]
        assert f'aria-label="{label.capitalize()}"' in classes
        assert "after:content-[attr(aria-label)]" in classes and "after:hidden" in classes
        assert "hover:after:block" in classes and "focus-visible:after:block" in classes
        assert "after:bottom-full" in classes and "after:pointer-events-none" in classes
    assert "after:left-1/2 after:-translate-x-1/2" in controls["history"]
    assert "after:right-0" in controls["remove"] and "after:left-1/2" not in controls["remove"]
    assert 'hx-get="/x?a=1&amp;b=2"' in desktop and 'data-js="dialog-open"' in desktop
    assert 'data-row-menu-button data-js="row-menu" data-subject=".env.db"' in phone
    assert 'aria-label="More actions for .env.db"' in phone and "md:hidden" in phone
    assert 'data-tone="danger"' in desktop.split('id="env-1-remove"')[1].split(">")[0]
    assert re.findall(r'data-row-action="([a-z]+)"', desktop) == ["edit", "history", "remove"]


def test_the_shared_row_menu_is_one_empty_dialog_for_the_shell() -> None:
    """The menu is filled by the page script, so the page holds it once and no row repeats it."""
    menu = render_snippet("{% from 'macros/row_actions.html' import row_menu %}{{ row_menu() }}")
    assert menu.startswith('<dialog id="row-menu" data-row-menu aria-label="Actions"')
    assert "data-row-menu-title" in menu and "data-row-menu-items" in menu
    assert "data-row-menu-item=" not in menu and "md:hidden" in menu


def test_a_single_row_action_is_one_control_with_its_text_on_a_phone() -> None:
    """One action is one element at every width: text on a phone, an icon with a tooltip above."""
    only = [{**LINKS[2], "key": "revoke", "label": "Revoke"}]
    page = render_snippet(ROW_ACTIONS, actions=only)
    assert page.count("data-row-action=") == 1 and "<dialog" not in page
    assert 'aria-label="Revoke"' in page and '<span class="md:sr-only">Revoke</span>' in page
    assert "md:hover:after:block" in page and "md:focus-visible:after:block" in page
    assert "after:content-[attr(aria-label)]" in page and "after:right-0" in page
    primary = render_snippet(ROW_ACTIONS, actions=[LINKS[0]])
    assert primary.count("<svg") == 0 and ">Edit</a>" in primary and "aria-label" not in primary
    assert render_snippet(ROW_ACTIONS, actions=[]) == ""


def test_row_actions_use_no_inline_style_script_or_handler() -> None:
    """Strict CSP: nothing but classes and data attributes."""
    page = render_snippet(ROW_ACTIONS, actions=LINKS)
    assert not re.search(r"\sstyle=|\sonclick=|<script|javascript:|\stitle=", page)


def test_every_page_title_shares_one_size() -> None:
    """The page header, sign-in card and error page take the title classes from one macro."""
    plain = render_snippet("{% from 'macros/panels.html' import h1_class %}{{ h1_class() }}")
    assert plain == "text-[26px] font-bold leading-tight md:text-3xl"
    mono = render_snippet("{% from 'macros/panels.html' import h1_class %}{{ h1_class(true) }}")
    assert mono == "font-mono " + plain


def test_no_page_template_sets_its_own_title_size() -> None:
    """A page title always takes its classes from `h1_class`; mail templates are not pages."""
    templates = Path(__file__).parents[1] / "templates"
    own_size = re.compile(r"<h1[ >](?!(?:class=\"\{\{ h1_class\())")
    offenders = [
        path.relative_to(templates).as_posix()
        for path in templates.rglob("*.html")
        if "mail" not in path.parts and own_size.search(path.read_text())
    ]
    assert offenders == []
