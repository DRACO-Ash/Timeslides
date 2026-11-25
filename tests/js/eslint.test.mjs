/* eslint.config.mjs carries two local rules, which are code like any other:
 * the gate counts their lines, and a rule that misfires is worse than none.
 * Each is driven here with hand-built syntax nodes, the shape ESLint hands it.
 */
import path from "node:path";
import {pathToFileURL} from "node:url";

import {ROOT} from "./dom.mjs";
import {assert, equal, test} from "./harness.mjs";

const config = (await import(pathToFileURL(path.join(ROOT, "eslint.config.mjs")).href)).default;
const plugin = config.find(block => block.plugins?.local).plugins.local;

const id = name => ({type: "Identifier", name});
const member = (object, property, computed = false) => (
  {type: "MemberExpression", object, property: id(property), computed});
const call = callee => ({type: "CallExpression", callee});
const and = (left, right, operator = "&&") => ({type: "LogicalExpression", operator, left, right});

function reports(ruleName, visit) {
  const found = [];
  const context = {
    report: r => found.push(r.message),
    sourceCode: {getAncestors: node => node.ancestors || []},
  };
  const handlers = plugin.rules[ruleName].create(context);
  visit(handlers);
  return found;
}

test("eslint: the optional-chain rule finds x && x.y at any depth of the chain", () => {
  const fires = node => reports("prefer-optional-chain", h => h.LogicalExpression(node)).length;
  equal(fires(and(id("v"), member(member(id("v"), "traces"), "length"))), 1, "v && v.traces.length");
  equal(fires(and(id("x"), call(member(id("x"), "y")))), 1, "x && x.y()");
  equal(fires(and(member(id("a"), "b"), member(member(id("a"), "b"), "c"))), 1, "a.b && a.b.c");
  equal(fires(and({type: "ThisExpression"}, member({type: "ThisExpression"}, "y"))), 1, "this && this.y");
  equal(fires(and(id("x"), call(id("x")))), 1, "x && x() is x?.(), the same finding");
});

test("eslint: the optional-chain rule leaves guards and unrelated pairs alone", () => {
  const fires = node => reports("prefer-optional-chain", h => h.LogicalExpression(node)).length;
  equal(fires(and(id("g"), member(id("window"), "Plotly"))), 0, "g && window.Plotly");
  equal(fires(and(id("x"), member(id("x"), "y"), "||")), 0, "x || x.y");
  equal(fires(and(id("x"), {type: "BinaryExpression"})), 0, "x && x.y !== z");
  equal(fires(and(member(id("a"), "b", true), member(member(id("a"), "b", true), "c"))), 0,
        "computed members are not compared");
  equal(fires(and(member(id("a"), "b"), member(member(id("a"), "c"), "d"))), 0, "a.b && a.c.d");
  equal(fires(and({type: "Literal"}, member(id("x"), "y"))), 0, "a literal guard");
});

test("eslint: the nested-conditional rule sees through calls but stops at functions", () => {
  const ternary = ancestors => ({type: "ConditionalExpression", ancestors});
  const fires = node => reports("no-nested-conditional", h => h.ConditionalExpression(node)).length;
  equal(fires(ternary([{type: "ConditionalExpression"}, {type: "NewExpression"}])), 1);
  equal(fires(ternary([{type: "ConditionalExpression"}, {type: "ArrowFunctionExpression"}])), 0);
  equal(fires(ternary([{type: "Program"}])), 0);
  assert(plugin.rules["no-nested-conditional"].meta.type === "suggestion");
});
