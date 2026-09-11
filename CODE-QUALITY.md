# Code quality: the findings, and what now stops them

Bluestaq Limited · Timeslides

Every rule in this document is a finding the App Store quality gate actually
raised against this project, or the direct generalisation of one. Each cost an
upload, a pipeline run and a round trip to discover. They are written down here
so the reasoning survives, and enforced in code so the next one does not need
discovering at all.

## Why this exists

The gate is SonarQube and it runs in the pipeline, which means it can only tell
you about a problem **after** an upload. Six separate rounds of this project
went: upload, wait, read one finding, fix it, upload again. That is a poor use
of a pipeline and a worse use of an afternoon.

The pattern in those findings is the useful part. `ruff.toml` had covered
Python from the start and says in its own header that it cannot see the CSS or
the JavaScript. **Every gate finding after that file was written landed in
exactly that blind spot.** The gap was not knowledge, it was that nothing ran.

## The three layers, and which one is the guarantee

| Layer | Covers | Runs where | Status |
|---|---|---|---|
| `ruff.toml` | Python | local, `ruff check .` | pre-flight |
| `eslint.config.mjs` | JavaScript, deeply | local, `npx eslint timeslides/report/assets` | pre-flight |
| `tests/test_code_standards.py` | CSS, this application's own rules, a JavaScript backstop | **everywhere, including the platform's test stage** | the guarantee |

The third layer is pure standard library on purpose. The pipeline's test stage
installs `requirements.txt` and runs `pytest`, nothing else, so a check that
needs a tool installed is a check that will not run there. Anything that must
not regress belongs in that file.

A rule earns a place only if it is mechanically checkable with close to no
false positives. **A check that cries wolf gets switched off, and then it
protects nothing.** Two rules in that file were rewritten during this work
because the first version flagged its own explanatory comment.

## The register

### JavaScript

| Finding | Rule | Enforced by |
|---|---|---|
| "Prefer using an optional chain expression" (twice) | `x && x.y` is `x?.y` | eslint local rule, `find_redundant_guards` |
| "Handle this exception or don't catch it at all" | no empty catch, no unread catch binding | `no-empty`, `no-unused-vars`, `find_empty_catches`, `find_unread_catch_bindings` |
| "The empty object is useless" | no empty literal spread or merged | `prefer-object-spread`, `find_object_assign` |
| Bare `parseInt` | use the `Number` namespace | `no-restricted-globals`, `find_bare_number_globals` |
| `Object.assign` where a spread belongs | use `{...a, ...b}` | `prefer-object-spread`, `find_object_assign` |
| — | no unary-plus coercion; `Number(x)` says what it does | `no-implicit-coercion`, `find_unary_plus_coercions` |
| — | no `var` | `no-var`, `find_var_declarations` |

**The optional-chain rule is the one to read carefully.** It applies to the
redundant form only, where the guard and the access are the same thing. It does
**not** apply to `sel && sel.value !== st.ref`: rewritten as `sel?.value !== st.ref`,
a null `sel` yields `undefined`, the comparison is then true, and the body runs
on the null. `report.js` contains one of those and it is correct as written. A
first attempt at this rule used an esquery selector, which cannot compare two
fields of a node and so flagged `g && window.Plotly`, where the identifiers are
unrelated. It is now a local ESLint rule that compares the two sides.

### CSS

| Finding | Rule | Enforced by |
|---|---|---|
| Duplicate selector | one top-level rule per selector | `find_duplicate_selectors` |
| — | every `!important` carries a comment saying why | `find_unexplained_important` |

`!important` is not banned. Beating an inline style that JavaScript sets, or
honouring a reduced-motion preference whatever else the sheet says, are both
right answers. The rule is that the author has to be able to say which, so a
comment above it has to.

A rule inside `@media` is a different context and is how an override is meant
to be written, so the duplicate check is deliberately top-level only.

### Python

| Finding | Rule | Enforced by |
|---|---|---|
| "Avoid binding the application to all network interfaces" | the host is configuration, in one place, with the reason recorded | `test_the_listen_address_is_configuration_rather_than_a_literal` |
| "Change this code to not construct the path from user-controlled data" | `STORAGE_MOUNT_PATH` validated at the boundary | `_storage_path`, `tests/test_config.py` |
| "This regex is vulnerable to exponential backtracking" | no repeat inside a repeat | `find_nested_quantifiers` |
| `dict()` constructor | use a literal | ruff `C408` |
| Multi-call `pytest.raises` | one call per block | ruff `PT012` family, review |
| Cognitive complexity over 15 | — | ruff `C90`, `max-complexity = 15` |
| Naming, unused arguments, FastAPI `Annotated` | — | ruff `N`, `ARG`, `B` |
| — | no `except` that only passes; `contextlib.suppress` says so | `find_except_that_only_passes` |

### Rules specific to this application

These are not general style. Each is something that was true of Timeslides,
cost a release to learn, and could not have been caught by a general linter.

| What happened | Rule | Enforced by |
|---|---|---|
| Report writes and group writes had separate implementations; the S3 fix landed in one and every render died in the other | every filesystem write goes through `timeslides/storage.py` | `tests/test_nonposix_e2e.py::test_only_the_storage_module_writes_to_the_filesystem` |
| `&middot;` appeared on screen verbatim in a tooltip | no named HTML entity in a Plotly hovertemplate; it decodes only the basic ones | `test_no_named_html_entity_in_a_plotly_hovertemplate` |
| The data-quality band was given the shell's `.err` and `.note`; the report does not load `shell.css` | every class used in a page exists in a stylesheet that page loads | `test_every_class_the_report_uses_exists_in_the_stylesheet_it_loads` |

## The rule about the rules

**A test that passes on a clean repository is exactly what a broken detector
also does.** Every detector in `tests/test_code_standards.py` is therefore a
named function, shown both the offender it was written for and the lookalike it
must leave alone. A further test asserts that no detector exists without such a
pair, because a detector nobody calls is a rule nobody enforces.

That principle is the same one behind the browser suite skipping loudly rather
than passing silently when no Chromium is present, and behind the non-POSIX
end-to-end suite running a real server rather than patching a module. Mapping
"could not verify" onto "passed" is the defect that hides every other one.

## Running them

```bash
# Python
.venv/bin/ruff check .

# JavaScript, if eslint is available
npx eslint timeslides/report/assets

# The guarantee, and everything else
.venv/bin/python -m pytest tests/test_code_standards.py
```

All three are part of the verification loop in `AUDIT.md`, which is what runs
before an upload.
