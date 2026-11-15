import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";

export default tseslint.config(
  { ignores: ["dist", "screenshots", "node_modules", "e2e"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: { globals: { ...globals.browser } },
    plugins: { "react-hooks": reactHooks },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      // API text is untrusted: never inject raw HTML.
      "no-restricted-syntax": ["error", { selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']", message: "Never render raw HTML." }],
    },
  },
  {
    // The UI must not import anything wallet specific outside src/wallet (only the context/barrel).
    files: ["src/components/**", "src/views/**", "src/hooks/**", "src/App.tsx"],
    rules: {
      "no-restricted-imports": ["error", { patterns: [
        "**/wallet/LocalDevWalletProvider*", "**/wallet/PrivyWalletProvider*", "@noble/*", "@privy-io/*",
      ] }],
    },
  },
);
