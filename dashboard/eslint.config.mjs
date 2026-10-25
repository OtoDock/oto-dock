// The dashboard's lint gate (`npm run lint`). Every rule is an error. The
// findings the code already had when the gate was adopted are frozen in
// eslint-suppressions.json, per file and rule with their count: a new finding
// fails, and so does a count that drops without the file being pruned
// (`npx eslint src --prune-suppressions`), so the frozen set only shrinks.
// Never `--suppress-all` to admit a new finding.
import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";
import noRawPalette from "./design/lint/no-raw-palette.mjs";

const hooksRules = Object.fromEntries(
  Object.keys(reactHooks.configs["recommended-latest"].rules).map((rule) => [rule, "error"]),
);

export default tseslint.config(
  // A test beside the hook it pins is a test like those under src/tests.
  { ignores: ["dist/**", "android/**", "src/tests/**", "src/**/*.test.{ts,tsx}", "**/*.d.ts"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
    linterOptions: { reportUnusedDisableDirectives: "error" },
    plugins: { "react-hooks": reactHooks, "react-refresh": reactRefresh },
    rules: {
      ...hooksRules,
      "react-refresh/only-export-components": ["error", { allowConstantExport: true }],
      "no-unused-vars": "off",
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_", caughtErrorsIgnorePattern: "^_" },
      ],
      "no-console": "error",
      "no-alert": "error",
    },
  },
  {
    // The one console writer: every breadcrumb and the few failures that
    // must never pass unseen go through it.
    files: ["src/lib/debug/log.ts"],
    rules: { "no-console": "off" },
  },
  {
    // Colours come from the semantic tokens (design/tokens.json). The files
    // left out hold palettes that are not the interface's: the terminal's
    // ANSI colours, the agent colour presets, the three.js map scene, and
    // the generated constants. A palette inside a file that also draws the
    // interface (the syntax theme, the map's backdrop) is exempted line by
    // line, so the rest of the file stays linted.
    files: ["src/**/*.{ts,tsx}"],
    ignores: [
      "src/hooks/useInteractiveTerminal.ts",
      "src/pages/agent/AgentConfig.presets.ts",
      "src/components/agents-map/**/*.ts",
      "src/uikit/tokens.generated.ts",
    ],
    plugins: { otodock: { rules: { "no-raw-palette": noRawPalette } } },
    rules: { "otodock/no-raw-palette": "error" },
  },
);
