"""The compendium of quality-gate findings, as executable rules.

Every rule here is a finding this project actually received from the App Store
quality gate, or the direct generalisation of one. Each cost an upload, a
pipeline run and a round trip to discover. Writing them down as prose would not
stop the next one; running them on every test run does.

Three layers cover this repository, and it matters which is which:

  ruff        Python. Configured in ruff.toml, run as a local pre-flight. Its
              own header says it cannot see the CSS or the JavaScript.
  eslint      JavaScript, deeply, including a local rule for the optional-chain
              finding. Configured in eslint.config.mjs. A pre-flight too: it
              needs eslint installed, which the pipeline's test stage does not
              have.
  this file   The guarantee. Pure standard library, so it runs in the
              platform's test stage exactly as it runs here, and cannot be
              skipped for want of a tool. It covers the CSS, the rules specific
              to this application, and a backstop for the JavaScript patterns
              that have actually cost uploads.

A rule belongs here only if it is mechanically checkable with close to no false
positives. A check that cries wolf gets switched off, and then it protects
nothing.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "timeslides"
ASSETS = PACKAGE / "report" / "assets"


def _js_files():
    return sorted(ASSETS.glob("*.js"))


def _py_files():
    return sorted(PACKAGE.rglob("*.py"))


def _strip_js_comments(text: str) -> str:
    """Blank out comments and string bodies so a pattern in prose is not a hit.

    Lengths are preserved, so a line number computed from an offset still
    points at the right line.
    """
    out, i, n = [], 0, len(text)
    while i < n:
        two = text[i:i + 2]
        if two == "/*":
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append(re.sub(r"[^\n]", " ", text[i:end]))
            i = end
        elif two == "//":
            end = text.find("\n", i)
            end = n if end == -1 else end
            out.append(" " * (end - i))
            i = end
        elif text[i] in "\"'`":
            quote, j = text[i], i + 1
            while j < n and text[j] != quote:
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
            out.append(quote + re.sub(r"[^\n]", " ", text[i + 1:j - 1]) + quote
                       if j - 1 > i else text[i:j])
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _hits(pattern, text, flags=0):
    """[(line, matched text)] for a pattern, ignoring comments and strings."""
    blanked = _strip_js_comments(text)
    return [(blanked.count("\n", 0, m.start()) + 1, m.group(0))
            for m in re.finditer(pattern, blanked, flags)]


# --------------------------------------------------------------------------- #
#  The detectors
#
#  Each is a function over text rather than a loop inside a test, so that the
#  tests further down can feed it a known offender and prove it fires. A check
#  that passes because its detector is broken protects nothing, which is the
#  same fail-open defect as a skipped browser suite counting as a pass.
# --------------------------------------------------------------------------- #
def find_redundant_guards(text: str) -> list:
    out = []
    for line, hit in _hits(r"\b([A-Za-z_$][\w$]*) && \1[.\[]", text):
        out.append((line, hit.strip()))
    return out


def find_empty_catches(text: str) -> list:
    return [(line, hit.strip()) for line, hit in
            _hits(r"catch\s*(\([^)]*\))?\s*\{\s*\}", text)]


def find_unread_catch_bindings(text: str) -> list:
    blanked = _strip_js_comments(text)
    out = []
    for m in re.finditer(r"catch\s*\(\s*([A-Za-z_$][\w$]*)\s*\)\s*\{", blanked):
        name = m.group(1)
        depth, i = 0, m.end() - 1
        while i < len(blanked):
            if blanked[i] == "{":
                depth += 1
            elif blanked[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if not re.search(rf"\b{re.escape(name)}\b", blanked[m.end():i]):
            out.append((blanked.count("\n", 0, m.start()) + 1, name))
    return out


def find_bare_number_globals(text: str, name: str) -> list:
    return [(line, hit.strip()) for line, hit in
            _hits(rf"(?<![.\w]){name}\s*\(", text)]


def find_object_assign(text: str) -> list:
    return [(line, hit.strip()) for line, hit in
            _hits(r"Object\.assign\s*\(\s*\{\s*\}", text)]


def find_unary_plus_coercions(text: str) -> list:
    return [(line, hit.strip()) for line, hit in
            _hits(r"[(,=\s]\+[A-Za-z_$][\w$.]*", text)]


def find_var_declarations(text: str) -> list:
    return [(line, hit.strip()) for line, hit in _hits(r"\bvar\s+[A-Za-z_$]", text)]


def find_duplicate_selectors(css: str) -> list:
    return sorted(s for s, n in Counter(_top_level_selectors(css)).items() if n > 1)


def find_unexplained_important(css: str) -> list:
    code = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)),
                  css, flags=re.S)
    raw_lines, code_lines = css.splitlines(), code.splitlines()
    out = []
    for i, line in enumerate(code_lines):
        if "!important" in line and "*/" not in "\n".join(raw_lines[max(0, i - 5):i]):
            out.append((i + 1, raw_lines[i].strip()))
    return out


def find_except_that_only_passes(source: str) -> list:
    return [node.lineno for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ExceptHandler) and len(node.body) == 1
            and isinstance(node.body[0], ast.Pass)]


def find_nested_quantifiers(source: str) -> list:
    out = []
    for m in re.finditer(r"""re\.compile\(\s*r?['"](.+?)['"]""", source):
        for group in re.findall(r"\((?:\?:)?[^()]*\)[*+]", m.group(1)):
            if re.search(r"[*+][^()]*\)[*+]$", group):
                out.append((source.count("\n", 0, m.start()) + 1, group))
    return out


# --------------------------------------------------------------------------- #
#  JavaScript
# --------------------------------------------------------------------------- #
def test_no_redundant_guard_where_an_optional_chain_belongs():
    """Gate finding, twice: "Prefer using an optional chain expression."

    Matched on the same identifier appearing on both sides, which is the
    redundant form the rule is about. `g && window.Plotly` is two different
    things and is left alone, as is `x && x.y !== z`, which is a guard: with x
    null an optional chain there yields undefined, the comparison is then true,
    and the body runs on a null.
    """
    found = [f"{p.name}:{line}: {hit}" for p in _js_files()
             for line, hit in find_redundant_guards(p.read_text(encoding="utf-8"))]
    assert found == [], "use x?.y rather than x && x.y:\n" + "\n".join(found)


def test_no_empty_catch_block():
    """Gate finding: "Handle this exception or don't catch it at all."

    A catch that does nothing hides the failure it was written to notice. Doing
    something can be as small as assigning a fallback, but it has to be there.
    """
    found = [f"{p.name}:{line}" for p in _js_files()
             for line, _hit in find_empty_catches(p.read_text(encoding="utf-8"))]
    assert found == [], "empty catch blocks:\n" + "\n".join(found)


def test_no_unread_catch_binding():
    """The same finding in its other shape: an error is named and never read,
    which says it was meant to be handled and was not. ES2019 allows `catch {}`
    with no binding, which states the intent honestly."""
    found = [f"{p.name}:{line}: '{name}' is never read" for p in _js_files()
             for line, name in find_unread_catch_bindings(
                 p.read_text(encoding="utf-8"))]
    assert found == [], ("name the error only if you use it, else `catch {}`:\n"
                         + "\n".join(found))


@pytest.mark.parametrize("name", ["parseInt", "parseFloat", "isNaN", "isFinite"])
def test_the_number_namespace_is_used_rather_than_the_bare_global(name):
    """Gate finding: the global forms are the legacy ones and coerce their
    argument differently from the Number namespace."""
    found = [f"{p.name}:{line}" for p in _js_files()
             for line, _hit in find_bare_number_globals(
                 p.read_text(encoding="utf-8"), name)]
    assert found == [], f"use Number.{name}:\n" + "\n".join(found)


def test_no_object_assign_where_a_spread_belongs():
    """Gate finding: Object.assign into a fresh literal is a spread."""
    found = [f"{p.name}:{line}" for p in _js_files()
             for line, _hit in find_object_assign(p.read_text(encoding="utf-8"))]
    assert found == [], "use {...a, ...b}:\n" + "\n".join(found)


def test_no_implicit_numeric_coercion_with_a_unary_plus():
    """Number(x) says what it does; a leading + is easy to read straight past
    and easy to mistake for addition."""
    found = [f"{p.name}:{line}: {hit}" for p in _js_files()
             for line, hit in find_unary_plus_coercions(
                 p.read_text(encoding="utf-8"))]
    assert found == [], "use Number(x):\n" + "\n".join(found)


def test_no_var_declarations():
    found = [f"{p.name}:{line}" for p in _js_files()
             for line, _hit in find_var_declarations(p.read_text(encoding="utf-8"))]
    assert found == [], "use const or let:\n" + "\n".join(found)


# --------------------------------------------------------------------------- #
#  Things learnt about this application specifically
# --------------------------------------------------------------------------- #
def test_no_named_html_entity_in_a_plotly_hovertemplate():
    """Found by hovering a real point, after shipping it.

    Plotly draws its tooltip as SVG text and decodes only the basic entities,
    so a named one such as the middot appeared on screen verbatim. Nothing but
    a browser shows that, which is why it is a rule rather than something to
    re-discover.

    Scanned as parsed string literals rather than as text, because the first
    version of this check read the file raw and flagged the comment that
    explains the rule.
    """
    tree = ast.parse((PACKAGE / "report" / "builder.py").read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if "%{" not in node.value:        # the Plotly template marker
            continue
        for entity in re.findall(r"&[a-zA-Z]{2,10};", node.value):
            if entity not in ("&amp;", "&lt;", "&gt;", "&quot;"):
                found.append(f"line {node.lineno}: {entity}")
    assert found == [], (
        "Plotly decodes only the basic entities in a hovertemplate; use the "
        f"literal character: {found}")


def test_every_class_the_report_uses_exists_in_the_stylesheet_it_loads():
    """Found by looking at the rendered page, after writing the markup.

    The data-quality band was given the shell's .err and .note classes. The
    report does not load shell.css, so it would have rendered unstyled inside
    the frame. The two stylesheets are separate documents and a class from one
    means nothing in the other.
    """
    css = (ASSETS / "report.css").read_text(encoding="utf-8")
    defined = set(re.findall(r"\.([A-Za-z][\w-]*)", css))
    builder = (PACKAGE / "report" / "builder.py").read_text(encoding="utf-8")
    used = set()
    for m in re.finditer(r"""class=['"]([^'"{}]+)['"]""", builder):
        used.update(m.group(1).split())
    missing = sorted(c for c in used - defined if not c.startswith("js-"))
    assert missing == [], (
        "these classes are used in the report markup but defined nowhere in "
        f"report.css, which is the only stylesheet it loads: {missing}")


def test_the_shell_only_uses_classes_one_of_its_stylesheets_defines():
    """The same rule for the configuration page, which loads both."""
    css = "".join((ASSETS / name).read_text(encoding="utf-8")
                  for name in ("report.css", "shell.css"))
    defined = set(re.findall(r"\.([A-Za-z][\w-]*)", css))
    shell = (PACKAGE / "shell.py").read_text(encoding="utf-8")
    used = set()
    for m in re.finditer(r"""class=['"]([^'"{}]+)['"]""", shell):
        used.update(m.group(1).split())
    missing = sorted(used - defined)
    assert missing == [], f"classes with no rule anywhere: {missing}"


# --------------------------------------------------------------------------- #
#  CSS
# --------------------------------------------------------------------------- #
def _top_level_selectors(css: str):
    depth, buf, out = 0, "", []
    for ch in css:
        if ch == "{":
            if depth == 0:
                out.append(buf.strip())
            depth += 1
            buf = ""
        elif ch == "}":
            depth = max(0, depth - 1)
            buf = ""
        elif depth == 0:
            buf += ch
    cleaned = [re.sub(r"/\*.*?\*/", "", s, flags=re.S).strip() for s in out]
    return [s for s in cleaned if s and not s.startswith("@")]


@pytest.mark.parametrize("sheet", ["report.css", "shell.css"])
def test_no_duplicate_top_level_selector(sheet):
    """Gate finding: a selector written twice at the top level.

    The later rule silently governs any property the two share, so a pair like
    that works only while they happen not to overlap. A rule inside @media is a
    different context and is how an override is meant to be written, so the
    check is deliberately top-level only.
    """
    dupes = find_duplicate_selectors((ASSETS / sheet).read_text(encoding="utf-8"))
    assert dupes == [], f"{sheet} defines these twice: {dupes}"


@pytest.mark.parametrize("sheet", ["report.css", "shell.css"])
def test_important_is_used_only_where_it_is_explained(sheet):
    """!important is not banned; it is required to carry its reason.

    There are cases where it is the right answer: beating an inline style that
    JavaScript sets, or honouring a reduced-motion preference whatever else the
    sheet says. There are also cases where it is a specificity contest somebody
    lost and papered over. The difference is whether the author could say which,
    so the rule is that a comment immediately above has to.
    """
    unexplained = [f"{sheet}:{line}: {hit}" for line, hit in
                   find_unexplained_important(
                       (ASSETS / sheet).read_text(encoding="utf-8"))]
    assert unexplained == [], ("every !important needs a comment above it "
                               "saying why:\n" + "\n".join(unexplained))


# --------------------------------------------------------------------------- #
#  Python, beyond what ruff already enforces
# --------------------------------------------------------------------------- #
def test_no_regex_with_a_nested_quantifier():
    """Gate finding: a regex vulnerable to exponential backtracking.

    r"^/(?:[A-Za-z0-9._][A-Za-z0-9._-]*/?)*$" was measured at four times the
    work per added character, 2.8 seconds at 26. A group that both repeats and
    contains a repeat is ambiguous, which is the shape that explodes.
    """
    found = [f"{p.relative_to(ROOT)}:{line}: {group}" for p in _py_files()
             for line, group in find_nested_quantifiers(
                 p.read_text(encoding="utf-8"))]
    assert found == [], ("a repeat inside a repeat can backtrack "
                         "exponentially:\n" + "\n".join(found))


def test_no_except_that_only_passes():
    """The Python shape of the swallowed-exception finding."""
    found = [f"{p.relative_to(ROOT)}:{line}" for p in _py_files()
             for line in find_except_that_only_passes(
                 p.read_text(encoding="utf-8"))]
    assert found == [], ("an except that only passes hides what it caught; use "
                         "contextlib.suppress, which says so:\n" + "\n".join(found))


def test_the_listen_address_is_configuration_rather_than_a_literal():
    """Gate finding: "Avoid binding the application to all network interfaces."

    The platform requires it, so the answer is not to bind to loopback: it is
    that the address is read from configuration with the reason recorded, in
    one place, rather than written as a literal wherever a server is started.
    """
    found = []
    for path in [*_py_files(), ROOT / "app.py"]:
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r'"0\.0\.0\.0"', text):
            line = text.count("\n", 0, m.start()) + 1
            found.append(f"{path.relative_to(ROOT)}:{line}")
    assert found == [], ("read the host from Settings, which documents why it "
                         "is what it is:\n" + "\n".join(found))


# --------------------------------------------------------------------------- #
#  The detectors detect
#
#  Every test above passes on a clean repository, which is exactly what a
#  broken detector also does. Each rule is therefore shown the offender it was
#  written for, and the lookalike it must leave alone. Without this pair the
#  compendium would be decorative.
# --------------------------------------------------------------------------- #
def test_the_optional_chain_detector_finds_the_real_thing():
    assert find_redundant_guards("if (x && x.y) go();")
    assert find_redundant_guards("log(err && err.message);")
    assert find_redundant_guards("const a = opts && opts['k'];")


def test_the_optional_chain_detector_leaves_a_genuine_guard_alone():
    """`g && window.Plotly` is two unrelated things, and `sel && sel.value !== x`
    is a guard: rewritten as a chain, a null sel yields undefined, the
    comparison is then true, and the body runs on the null. report.js has one
    of those and it is right as written."""
    assert find_redundant_guards("if (g && window.Plotly) resize();") == []
    assert find_redundant_guards("if (a && b.c) go();") == []
    assert find_redundant_guards("const n = count && total;") == []


def test_the_optional_chain_detector_ignores_comments_and_strings():
    assert find_redundant_guards("/* avoid x && x.y here */") == []
    assert find_redundant_guards('const s = "x && x.y";') == []


def test_the_empty_catch_detector_finds_both_shapes():
    assert find_empty_catches("try { a(); } catch (e) {}")
    assert find_empty_catches("try { a(); } catch {}")
    assert find_empty_catches("try { a(); } catch (e) { log(e); }") == []


def test_the_unread_binding_detector_finds_a_named_but_unused_error():
    assert find_unread_catch_bindings("try { a(); } catch (err) { b = null; }")
    assert find_unread_catch_bindings("try { a(); } catch (err) { log(err); }") == []
    assert find_unread_catch_bindings("try { a(); } catch { b = null; }") == []


@pytest.mark.parametrize("name", ["parseInt", "parseFloat", "isNaN", "isFinite"])
def test_the_number_global_detector_finds_the_bare_form_only(name):
    assert find_bare_number_globals(f"const n = {name}(x, 10);", name)
    assert find_bare_number_globals(f"const n = Number.{name}(x, 10);", name) == []


def test_the_object_assign_detector():
    assert find_object_assign("const o = Object.assign({}, a, b);")
    assert find_object_assign("Object.assign(target, a);") == []


def test_the_unary_plus_detector():
    assert find_unary_plus_coercions("go(+el.dataset.obj);")
    assert find_unary_plus_coercions("go(Number(el.dataset.obj));") == []
    assert find_unary_plus_coercions("const n = a + b;") == []


def test_the_var_detector():
    assert find_var_declarations("var x = 1;")
    assert find_var_declarations("const x = 1;") == []


def test_the_duplicate_selector_detector():
    """The finding as it arrived: one selector, two rules, the later silently
    governing anything they share."""
    assert find_duplicate_selectors(".a{color:red}\n.a{animation:x}") == [".a"]
    assert find_duplicate_selectors(".a{color:red}\n.a:hover{color:blue}") == []


def test_the_duplicate_selector_detector_allows_a_media_query_override():
    """A rule inside @media is a different context and is how an override is
    meant to be written."""
    css = ".a{animation:x}\n@media (prefers-reduced-motion:reduce){.a{animation:none}}"
    assert find_duplicate_selectors(css) == []


def test_the_important_detector_wants_a_reason_not_abstinence():
    assert find_unexplained_important(".a{color:red!important}")
    assert find_unexplained_important("/* why */\n.a{color:red!important}") == []


def test_the_silent_except_detector():
    assert find_except_that_only_passes("try:\n    a()\nexcept OSError:\n    pass\n")
    assert find_except_that_only_passes(
        "try:\n    a()\nexcept OSError:\n    b = 1\n") == []


def test_the_nested_quantifier_detector_finds_the_pattern_that_cost_2_8_seconds():
    """The exact regex that was measured at four times the work per added
    character, 2.8 seconds at 26 characters."""
    source = 'X = re.compile(r"^/(?:[A-Za-z0-9._][A-Za-z0-9._-]*/?)*$")'
    assert find_nested_quantifiers(source)


def test_the_nested_quantifier_detector_leaves_a_flat_pattern_alone():
    assert find_nested_quantifiers(r'X = re.compile(r"[\x00-\x1f\x7f-\x9f]")') == []
    assert find_nested_quantifiers(r'X = re.compile(r"^[A-Za-z0-9._-]+$")') == []


def test_every_detector_in_this_file_is_exercised_by_a_test():
    """A detector nobody calls is a rule nobody enforces."""
    # Read from globals() rather than importing this module again: a module
    # that imports itself is its own finding.
    detectors = {n for n in globals() if n.startswith("find_")}
    source = Path(__file__).read_text(encoding="utf-8")
    # Split once, on the first occurrence: the marker also appears here, as
    # the literal below, so taking the last part gave only these few lines and
    # the check reported every detector as unexercised.
    tests_only = source.split("#  The detectors detect", 1)[1]
    unexercised = sorted(d for d in detectors if d not in tests_only)
    assert unexercised == [], f"no test feeds these a known offender: {unexercised}"
