import next from "eslint-config-next";
import coreWebVitals from "eslint-config-next/core-web-vitals";
import typescript from "eslint-config-next/typescript";

/**
 * ESLint 9 flat config. `eslint-config-next` ships flat arrays from v16 onward, so
 * nothing is adapted through `FlatCompat` — that path drags the legacy validator
 * back in and it cannot read this project's plugin set.
 *
 * The lint here is deliberately narrow: Next's core-web-vitals rules catch the
 * mistakes that actually cost an operator (an `<img>` that blocks the board, a
 * `Link` that navigates wrongly), and `next/typescript` catches the type-shaped
 * ones. Style preferences are not encoded.
 */
const eslintConfig = [
  {
    ignores: [".next/**", "out/**", "build/**", "node_modules/**", "next-env.d.ts"],
  },
  ...next,
  ...coreWebVitals,
  ...typescript,
  {
    rules: {
      // An underscore-prefixed binding is this project's spelling for "deliberately
      // not carried forward" — used when a destructure drops a secret field (§19).
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
    },
  },
];

export default eslintConfig;
