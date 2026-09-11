/* Local pre-flight for the JavaScript the App Store quality gate reads.
 *
 * ruff.toml does this job for Python and says in its own header that it cannot
 * see the CSS or the JavaScript. Every gate finding since that file was
 * written landed in exactly that blind spot: an empty object literal, a
 * bare parseInt, an Object.assign that wanted a spread, a swallowed catch, and
 * twice an "x && x.y" that wanted an optional chain. Each cost an upload and a
 * pipeline run to discover.
 *
 * This is not a substitute for the gate, for the same reason ruff is not: the
 * gate is SonarQube and it decides. It is a way to see the same class of
 * finding in a second, on a laptop, before an upload.
 *
 *     npx eslint timeslides/report/assets
 *
 * The rules below are not a general style opinion. Each one is a finding this
 * project actually received, or the direct generalisation of one.
 */
/* A local rule for the one thing esquery cannot express.
 *
 * The gate's optional-chain finding is about the REDUNDANT form, where the
 * guard and the access are the same thing: `x && x.y`, which is `x?.y`. A
 * selector cannot compare two fields of the same node, so a selector-based
 * version flagged `g && window.Plotly`, where the two identifiers are
 * unrelated and there is nothing redundant at all.
 *
 * It also deliberately leaves `x && x.y !== z` alone. That is a guard, not a
 * redundant chain, and rewriting it as `x?.y !== z` changes behaviour: with x
 * null the chain yields undefined, the comparison is then true, and the body
 * runs on a null. report.js has one of those and it is right as written.
 */
function sameSource(a, b) {
  if (a.type === "Identifier" && b.type === "Identifier") return a.name === b.name;
  if (a.type === "ThisExpression" && b.type === "ThisExpression") return true;
  if (a.type === "MemberExpression" && b.type === "MemberExpression") {
    return !a.computed && !b.computed &&
           a.property.name === b.property.name &&
           sameSource(a.object, b.object);
  }
  return false;
}

const local = {
  rules: {
    "prefer-optional-chain": {
      meta: { type: "suggestion", docs: { description: "x && x.y is x?.y" } },
      create(context) {
        return {
          LogicalExpression(node) {
            if (node.operator !== "&&") return;
            const right = node.right;
            if (right.type !== "MemberExpression" && right.type !== "CallExpression") return;
            const target = right.type === "CallExpression" ? right.callee : right.object;
            if (!target) return;
            const base = right.type === "CallExpression"
              ? (target.type === "MemberExpression" ? target.object : target)
              : target;
            if (!sameSource(node.left, base)) return;
            context.report({
              node,
              message: "Use an optional chain (x?.y) rather than x && x.y.",
            });
          },
        };
      },
    },
  },
};

export default [
  {
    files: ["timeslides/report/assets/**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: {
        document: "readonly", window: "readonly", console: "readonly",
        fetch: "readonly", setTimeout: "readonly", clearTimeout: "readonly",
        setInterval: "readonly", clearInterval: "readonly",
        Plotly: "readonly", URL: "readonly", Set: "readonly", Map: "readonly",
      },
    },
    plugins: { local },
    linterOptions: { reportUnusedDisableDirectives: "error" },
    rules: {
      /* Swallowed exceptions. Gate finding: "Handle this exception or don't
         catch it at all." A catch that does nothing hides the failure it was
         written to notice. */
      "no-empty": ["error", { allowEmptyCatch: false }],

      /* Unused bindings, including the catch parameter. Naming a caught error
         and never using it is the shape the rule above objects to. */
      "no-unused-vars": ["error", {
        args: "after-used", caughtErrors: "all",
        varsIgnorePattern: "^_", argsIgnorePattern: "^_",
      }],

      /* Gate finding: Object.assign where a spread reads better. */
      "prefer-object-spread": "error",

      /* Gate finding: the global parseInt and friends. Number.parseInt is the
         same function with an unambiguous origin. */
      "no-restricted-globals": ["error",
        { name: "parseInt", message: "Use Number.parseInt." },
        { name: "parseFloat", message: "Use Number.parseFloat." },
        { name: "isNaN", message: "Use Number.isNaN." },
        { name: "isFinite", message: "Use Number.isFinite." },
      ],

      /* Gate findings, twice: "Prefer using an optional chain expression."
         See the rule definition above for why this is not a selector. */
      "local/prefer-optional-chain": "error",

      /* Gate finding: "The empty object is useless." An empty literal spread
         or merged into another contributes nothing and reads as an oversight. */
      "no-useless-computed-key": "error",
      "no-useless-concat": "error",
      "no-useless-return": "error",
      "no-lonely-if": "error",
      "no-else-return": "error",

      /* Correctness rules that cost nothing and catch real mistakes. */
      "eqeqeq": ["error", "smart"],
      "no-var": "error",
      "prefer-const": "error",
      "no-implicit-coercion": ["error", { allow: ["!!"] }],
      "no-throw-literal": "error",
      "require-atomic-updates": "error",
      "no-await-in-loop": "off",
      "no-console": "off",
    },
  },
];
