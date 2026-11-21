# How we write

These rules cover every page OtoDock writes for people. That is the pages of
this repository, the docs site, the website and the three community catalogs.
A program checks what a program can check: `scripts/prose/prose_check.py`. The
rest is on the writer. Each repository's contributing page gives the command
to run before you push.

## The rules

1. **One idea in a sentence.** Thirty words at most, and most far fewer. Past
   twenty, look for the second idea and give it its own sentence.
2. **Short paragraphs.** Five sentences or eighty words at most. Split where
   the subject changes. A list is often the better shape.
3. **No dashes and no semicolons.** Write a full stop, a comma or a colon.
4. **No emoji and no decorative symbols.** An arrow is for a path through
   menus only: **Settings → Integrations**. On a screen and on the website,
   name the place in words.
5. **No version numbers.** A page says how things work today, and a number is
   stale after the next release. The version of other software a person
   installs is fine: Docker Engine 26. Our own pins live in `VERSIONS.md`.
6. **Today, not history.** Leave out "can now", "no longer", "used to" and
   "since" a version. The changelog says what changed.
7. **Only what the reader uses or must know.** Say what it is, what you do,
   and give an everyday example if it helps. Then stop. When in doubt, cut.
8. **One name for one thing.** Pick the word the product uses on screen and
   keep it on every page.

The check fails a page on rules 1 and 3 to 5. It lets → pass anywhere,
since it cannot tell a menu path. It fails a page for people who use OtoDock
on rule 6 too. Rules 7 and 8 are yours.

A sentence past twenty words and a long paragraph are advice. A full run
counts them in one line, and `--file` lists them.

## Two kinds of page

A page for people who **use** OtoDock is read by someone who does not know
the code. The README, the docs, the changelog and a catalog entry are such
pages. It says what the person sees or does, never how it works inside. It
names no AI model, and no small part of a screen such as a badge or a toast.

A page for people who **build** OtoDock may use the terms of the trade. The
contributing guide, the architecture page and the protocol reference are such
pages. Rules 1 to 5 hold there too. A reference may state the version in
which something arrived, when its readers act on that.

A repository with a `.prose.json` at its root runs the check in its release
gate. Where that file is public, the check runs on every pull request too.
The file lists the repository's pages of each kind. A markdown file that the
list does not name fails the check. So a new page is always sorted on
purpose.

## When the check is wrong

A rule can misread a sentence. Do not bend the sentence until it reads worse.
Add the exact words to `allowed` in `.prose.json`, with the rule and the
reason. A reviewer reads that line.

## Examples

A dash, and a sentence about how it works inside:

```text
Notion's OAuth doesn't grant access to your entire workspace — only the
pages you explicitly share with the integration during the consent flow. If
a tool call returns "Page not found", the user needs to share that page (or
its parent) with the OtoDock integration in Notion's UI.
```

The same, as it should read:

> Notion gives OtoDock only the pages you pick when you connect. If a tool
> answers "Page not found", share that page or its parent with the OtoDock
> integration in Notion.

One sentence that holds five rules with semicolons:

```text
Dashboard hooks pass the React compiler's lint rules without a new
eslint-disable: a latest-value ref is written in useLayoutEffect, never in
render or inside a state updater; a callback an effect registers once is a
useEffectEvent; state derived from a prop or reset on a key change is
adjusted in render (keep the source in state and set both when it changes),
not in an effect; a value from outside React (an element's box, a media
query) is a useSyncExternalStore store; and a hook's unstable return object
is destructured, its members listed in dependency arrays, never the object.
```

The same, as a list:

> Dashboard hooks pass the React compiler's lint rules without a new
> `eslint-disable`:
>
> - Write a latest-value ref in `useLayoutEffect`, never in render or inside
>   a state updater.
> - A callback that an effect registers once is a `useEffectEvent`.
> - Adjust state that derives from a prop, or resets on a key change, in
>   render and not in an effect. Keep the source in state and set both when it
>   changes.
> - A value from outside React, such as an element's box or a media query, is
>   a `useSyncExternalStore` store.
> - Destructure a hook's unstable return object. List its members in
>   dependency arrays, never the object.

History and version numbers:

```text
A Mac paired before 1.9.1 finds its CLIs again after the update alone:
from 0.5.160 the satellite puts Homebrew's and your own tool folders in
front of its PATH at start.
```

The same, as how it works today:

> When it starts, the satellite puts Homebrew's folders and your own tool
> folders first in its `PATH`.

A table cell that tries to say everything, shortened here:

```text
| install.sh | One-command bootstrap for the Docker quick start: preflights
Docker/Compose, installs into the directory it is run from (refuses $HOME),
writes .env with a generated Postgres password, handles the Ubuntu userns
step, fetches the release-pinned compose files and starts the stack. Fresh
installs only: it refuses to touch an existing one; idempotent on re-run
after a stop. | self-host operators |
```

The same, as one line in the table and a short section below it. Every fact
is still there:

> | `install.sh` | Installs OtoDock with Docker in the folder you run it from. | Admins of an install |
>
> **`install.sh`** checks Docker and Compose first, then installs into the
> folder you run it from and refuses your home folder. It writes `.env` with
> a new Postgres password and does the Ubuntu user namespace step. Then it
> fetches the compose files of the release and starts OtoDock. It is for a
> new install only and refuses to touch an existing one. After a stop you can
> run it again safely.
