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

const hooksRules = Object.fromEntries(
  Object.keys(reactHooks.configs["recommended-latest"].rules).map((rule) => [rule, "error"]),
);

export default tseslint.config(
  { ignores: ["dist/**", "android/**", "src/tests/**", "**/*.d.ts"] },
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
);
