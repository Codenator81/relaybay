# Keep a large spec in its file and read it in parts

A spec that changes with the code belongs next to the code, in its repository. relaybay can index
such a file in place, so a session reads one section of it instead of loading the whole text into
its context. relaybay keeps no copy of the file, and writes it only when that is turned on for
the document.

## Add the file

On **Docs & rules**, open **New document: a file on disk**, give the document a name (`spec`) and
the file's absolute path on the machine relaybay runs on. Or from a session:

```text
doc_update(project="acme-shop", doc="spec", body="-", reason="the spec, from its file",
           base_version=0, source_path="/home/me/src/acme-shop/docs/spec.md")
```

The file may be anywhere on the machine (a repository or a notes folder), must be UTF-8 and at
most 8 MB. A file on a network share needs that share in `attachment_roots`.

## Read it in parts

```text
doc_outline(project="acme-shop", doc="spec")
  - h2 #payments Payments · 120-2610 · 210k/60k
    - h3 #gift-cards (alias #gc) Gift cards · 2140-2610 · 52k/15k

doc_search(project="acme-shop", query="partial refund")
  - spec#gift-cards "Spec > Payments > Gift cards" · line 2188 · section lines 2140-2610 · ...

doc_get(project="acme-shop", doc="spec", section="gift-cards")
doc_get(project="acme-shop", doc="spec", lines="2180-2240")
```

A document over 200,000 characters is read at most 40,000 characters at a time (pass `max_chars`
for another cap). A longer part comes back as its first lines, the outline of what it holds and
the next call to make, never cut without saying so.

## Name a section so links survive

A heading's anchor changes when the heading is reworded. Put an explicit anchor above it:

```markdown
<a id="gc"></a>

### Gift cards
```

`relaybay://doc/acme-shop/spec#gc` on a card keeps pointing at that section. `task_context` shows
such a link as a pointer (title, lines, size, file and the `doc_get` call), not as text.

## Change it

Edit the file with your own tools, in your own working tree, and commit it with the code. By
default `doc_update` refuses a file document and names the file and the section's lines.

A coordinator session can let relaybay write the file for this document:

```text
doc_update(project="acme-shop", doc="spec", body="-", reason="sessions edit the spec here",
           base_version=1, file_writes="on")
doc_update(project="acme-shop", doc="spec", body="### Gift cards
...", reason="partial refunds",
           base_version=4, section="gift-cards")
```

Then `doc_update` writes a section, a line range or the whole text into the file: only the replaced
lines change (line endings, byte order mark and final newlines stay), so `git diff` shows just
that section. The write does not commit. A change made in the file by other means is a new
version: a writer with an older `base_version` gets the current section back to merge, and reads
say `Note: spec was changed outside relaybay at ...` until the next write through relaybay.

Either way, the next read
notices the change (size and time, then the hash), indexes the file again and records which
sections changed; `doc_history` lists those changes. A file that disappears keeps its document and
its last outline, marked "file missing since …", until it is back.

A line that a secret detector matches is masked in the index (the outline says how many); reads of
the file show its text as it is.
