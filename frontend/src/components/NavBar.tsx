/**
 * The navbar, built from the server's policy-filtered tree.
 *
 * This component deliberately renders nothing of its own accord about who can
 * see what. `/api/navigation` returns `navigation_for(actor)` -- the same call
 * the Jinja context processor makes -- so a link exists exactly when the route
 * behind it would admit this actor. Two implementations of that rule would
 * disagree the first time `policy.NAV_ENTRIES` or a route gate changed.
 *
 * Hiding a link is a usability affordance and never authorization. Every route
 * re-checks the gate and the module server-side, which is why this file has no
 * permission logic in it at all.
 */
import { useMemo, useRef, useState } from 'react';
import { NavLink, useLocation } from 'react-router-dom';

import type { NavChild, NavEntry } from '@/lib/types';

/**
 * Map a legacy page URL to a router path.
 *
 * The router is mounted with `basename="/app"` (see main.tsx), so react-router
 * prepends the prefix to every `to`/`path` itself. These must therefore stay
 * **bare** — the same `/dashboard`, `/admin/users` strings the server's nav
 * tree already uses.
 *
 * Returning `${href}` rather than `/app${href}` is not a style choice: doing
 * both produces `/app/app/dashboard`, which renders (the shell matches on the
 * stripped path) but writes a dead `href` into every link, so every client-side
 * navigation and every middle-click/open-in-new-tab goes 404.
 */
function hrefToRoute(href: string): string {
  return href || '/dashboard';
}

function isCurrent(pathname: string, href: string, exact: boolean): boolean {
  const target = hrefToRoute(href);
  return exact ? pathname === target : pathname === target || pathname.startsWith(`${target}/`);
}

/**
 * `navigation_for()` passes divider entries through unchanged (`{'divider':
 * True}`), and they carry no `href` or `label`. The Jinja navbar branches on
 * exactly that and renders an `<hr>`; rendering them as links instead produces
 * an item with an undefined label and a dead href, so the shape is narrowed
 * here rather than assumed.
 */
type NavChildOrDivider =
  | NavChild
  | { divider: true; href?: undefined; label?: undefined };

function isDivider(child: NavChildOrDivider): child is { divider: true } {
  return (child as { divider?: boolean }).divider === true;
}

function ChildLink({ child, pathname }: { child: NavChildOrDivider; pathname: string }) {
  if (isDivider(child)) {
    return <li role="separator" className="dropdown-divider" />;
  }

  return (
    <li>
      <NavLink
        to={hrefToRoute(child.href)}
        className={({ isActive }) => (isActive ? 'dropdown-item is-active' : 'dropdown-item')}
        aria-current={isCurrent(pathname, child.href, false) ? 'page' : undefined}
      >
        {child.label}
      </NavLink>
    </li>
  );
}

/**
 * The Modules group: a disclosure button plus its menu.
 *
 * Implements the WAI-ARIA disclosure pattern, because CSS alone cannot:
 *
 * - `hover`/`focus-within` reveals the menu for a mouse and a Tab key, but the
 *   menu is still `visibility`-less to a screen reader until something tells it
 *   the state. `aria-expanded` is what actually conveys it.
 * - Arrow keys move between items, Escape closes and returns focus to the
 *   trigger. Without Escape a keyboard user who opened the menu is trapped in
 *   it, which is the specific failure the ARIA pattern exists to prevent.
 */
function NavGroup({ entry, pathname }: { entry: NavEntry; pathname: string }) {
  const [open, setOpen] = useState(false);
  // How the menu came to be open. Hover and an explicit action are different
  // events with different intent, and collapsing them into one boolean is what
  // made clicking the trigger *close* a menu the pointer had just opened —
  // because a click is always preceded by a mouseenter.
  const openedBy = useRef<'hover' | 'explicit' | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLUListElement>(null);

  const items = useMemo(
    () =>
      ((entry.children ?? []) as NavChildOrDivider[]).filter(
        (c): c is NavChild => !isDivider(c),
      ),
    [entry.children],
  );

  const openFromHover = () => {
    if (openedBy.current === 'explicit') return;
    openedBy.current = 'hover';
    setOpen(true);
  };

  const closeFromHover = () => {
    // An explicitly opened menu stays open when the pointer leaves, so a
    // keyboard user who opened it with Enter is not closed by a mouse that
    // happened to be elsewhere.
    if (openedBy.current === 'explicit') return;
    openedBy.current = null;
    setOpen(false);
  };

  const openExplicitly = () => {
    openedBy.current = 'explicit';
    setOpen(true);
  };

  const closeExplicitly = () => {
    openedBy.current = null;
    setOpen(false);
  };

  const focusItem = (index: number) => {
    const links = menuRef.current?.querySelectorAll<HTMLAnchorElement>('.dropdown-item');
    if (!links?.length) return;
    // Wrap around, so ArrowDown on the last item returns to the first.
    const next = (index + links.length) % links.length;
    links[next]?.focus();
  };

  const onTriggerKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === 'ArrowDown' || event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      openExplicitly();
      // Focus lands on the first item after the menu renders.
      requestAnimationFrame(() => focusItem(0));
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      openExplicitly();
      requestAnimationFrame(() => focusItem(-1));
    }
  };

  const onMenuKeyDown = (event: React.KeyboardEvent<HTMLUListElement>) => {
    const links = Array.from(
      menuRef.current?.querySelectorAll<HTMLAnchorElement>('.dropdown-item') ?? [],
    );
    const current = links.indexOf(document.activeElement as HTMLAnchorElement);

    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        focusItem(current + 1);
        break;
      case 'ArrowUp':
        event.preventDefault();
        focusItem(current - 1);
        break;
      case 'Home':
        event.preventDefault();
        focusItem(0);
        break;
      case 'End':
        event.preventDefault();
        focusItem(links.length - 1);
        break;
      case 'Escape':
        // Close and return focus to the trigger. Tabbing out of an open menu
        // that is still announced as expanded is disorienting, and leaving
        // focus inside a hidden element strands the keyboard entirely.
        event.preventDefault();
        closeExplicitly();
        triggerRef.current?.focus();
        break;
      default:
        break;
    }
  };

  return (
    <li
      className="navbar__group"
      onMouseEnter={openFromHover}
      onMouseLeave={closeFromHover}
    >
      <button
        ref={triggerRef}
        type="button"
        className="navbar__link navbar__link--group"
        aria-expanded={open}
        aria-haspopup="true"
        aria-controls="navbar-modules-menu"
        /*
         * Toggles, except that it will not close a menu the *pointer* opened.
         * A click is always preceded by a mouseenter, so a plain toggle makes
         * the trigger look broken to every mouse user: the menu opens on the
         * way to the button and shuts on the button itself. The first click
         * "pins" it open; a second click closes it.
         */
        onClick={() => {
          if (open && openedBy.current === 'explicit') closeExplicitly();
          else openExplicitly();
        }}
        onKeyDown={onTriggerKeyDown}
      >
        {entry.label}
      </button>
      <ul
        id="navbar-modules-menu"
        ref={menuRef}
        className={`navbar__dropdown${open ? ' is-open' : ''}`}
        onKeyDown={onMenuKeyDown}
      >
        {(entry.children ?? []).map((child, index) => (
          <ChildLink
            key={isDivider(child) ? `divider-${index}` : child.href}
            child={child as NavChildOrDivider}
            pathname={pathname}
          />
        ))}
      </ul>
      <span className="sr-only" aria-live="polite">
        {open ? `${items.length} items available` : ''}
      </span>
    </li>
  );
}

export function NavBar({ entries }: { entries: NavEntry[] }) {
  const { pathname } = useLocation();

  return (
    <nav className="navbar" aria-label="Main">
      <div className="navbar__inner">
        <NavLink to="/dashboard" className="navbar__brand">
          <span className="navbar__logo" aria-hidden="true" />
          HRMS
        </NavLink>

        <ul className="navbar__links">
          {entries.map((entry) => {
            if (entry.group) {
              return <NavGroup key={entry.href} entry={entry} pathname={pathname} />;
            }
            return (
              <li key={entry.href}>
                <NavLink
                  to={hrefToRoute(entry.href)}
                  className={({ isActive }) => (isActive ? 'navbar__link is-active' : 'navbar__link')}
                  aria-current={isCurrent(pathname, entry.href, entry.href === '/dashboard') ? 'page' : undefined}
                >
                  {entry.label}
                </NavLink>
              </li>
            );
          })}
        </ul>
      </div>
    </nav>
  );
}