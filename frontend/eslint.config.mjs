import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";
export default defineConfig([
  ...nextVitals,
  ...nextTs,
  globalIgnores([
    ".next/**",
    "test-results/**",
    "playwright-report/**",
    "next-env.d.ts",
  ]),
  {
    files: ["app/**/*.{ts,tsx}", "components/**/*.{ts,tsx}"],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          patterns: [
            {
              group: ["**/fixtures/**", "**/fixture-api"],
              message: "Consume JobSiftApi; never import fixtures in UI.",
            },
          ],
        },
      ],
    },
  },
]);
